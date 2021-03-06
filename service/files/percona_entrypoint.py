#!/usr/bin/env python

import fileinput
import functools
import json
import logging
import os
import os.path
import shutil
import socket
import subprocess
import signal
import six.moves
import sys
import time

import etcd
import pymysql.cursors

HOSTNAME = socket.getfqdn()
IPADDR = socket.gethostbyname(HOSTNAME)
DATADIR = "/var/lib/mysql"
INIT_FILE = os.path.join(DATADIR, 'init.ok')
PID_FILE = os.path.join(DATADIR, "mysqld.pid")
GRASTATE_FILE = os.path.join(DATADIR, 'grastate.dat')
SST_FLAG = os.path.join(DATADIR, "sst_in_progress")
DHPARAM = os.path.join(DATADIR, "dhparams.pem")
GLOBALS_PATH = '/etc/ccp/globals/globals.json'
GLOBALS_SECRETS_PATH = '/etc/ccp/global-secrets/global-secrets.json'
CA_CERT = '/opt/ccp/etc/tls/ca.pem'

LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FORMAT = "%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s"
logging.basicConfig(format=LOG_FORMAT, datefmt=LOG_DATEFMT)
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

FORCE_BOOTSTRAP = None
FORCE_BOOTSTRAP_NODE = None
EXPECTED_NODES = None
MYSQL_ROOT_PASSWORD = None
CLUSTER_NAME = None
XTRABACKUP_PASSWORD = None
MONITOR_PASSWORD = None
CONNECTION_ATTEMPTS = None
CONNECTION_DELAY = None
ETCD_PATH = None
ETCD_HOST = None
ETCD_PORT = None
ETCD_TLS = None
DHPARAM_CERT = None


class ProcessException(Exception):
    def __init__(self, exit_code):
        self.exit_code = exit_code
        self.msg = "Command exited with code %d" % self.exit_code
        super(ProcessException, self).__init__(self.msg)


def retry(f):
    @functools.wraps(f)
    def wrap(*args, **kwargs):
        attempts = CONNECTION_ATTEMPTS
        delay = CONNECTION_DELAY
        while attempts > 1:
            try:
                return f(*args, **kwargs)
            except etcd.EtcdException as e:
                LOG.warning('Etcd is not ready: %s', str(e))
                LOG.warning('Retrying in %d seconds...', delay)
                time.sleep(delay)
                attempts -= 1
        return f(*args, **kwargs)
    return wrap


def merge_configs(variables, new_config):
    for k, v in new_config.items():
        if k not in variables:
            variables[k] = v
            continue
        if isinstance(v, dict) and isinstance(variables[k], dict):
            merge_configs(variables[k], v)
        else:
            variables[k] = v


def get_config():
    LOG.info("Getting global variables from %s", GLOBALS_PATH)
    variables = {}
    with open(GLOBALS_PATH) as f:
        global_conf = json.load(f)
    with open(GLOBALS_SECRETS_PATH) as f:
        secrets = json.load(f)
    merge_configs(global_conf, secrets)
    for key in ['percona', 'db', 'etcd', 'namespace', 'cluster_domain',
                'security']:
        variables[key] = global_conf[key]
    LOG.debug(variables)
    return variables


def set_globals():

    config = get_config()
    global MYSQL_ROOT_PASSWORD, CLUSTER_NAME, XTRABACKUP_PASSWORD
    global MONITOR_PASSWORD, CONNECTION_ATTEMPTS, CONNECTION_DELAY
    global ETCD_PATH, ETCD_HOST, ETCD_PORT, EXPECTED_NODES
    global FORCE_BOOTSTRAP, FORCE_BOOTSTRAP_NODE, ETCD_TLS, DHPARAM_CERT

    FORCE_BOOTSTRAP = config['percona']['force_bootstrap']['enabled']
    FORCE_BOOTSTRAP_NODE = config['percona']['force_bootstrap']['node']
    MYSQL_ROOT_PASSWORD = config['db']['root_password']
    CLUSTER_NAME = config['percona']['cluster_name']
    XTRABACKUP_PASSWORD = config['percona']['xtrabackup_password']
    MONITOR_PASSWORD = config['percona']['monitor_password']
    CONNECTION_ATTEMPTS = config['etcd']['connection_attempts']
    CONNECTION_DELAY = config['etcd']['connection_delay']
    EXPECTED_NODES = int(config['percona']['cluster_size'])
    ETCD_PATH = "/galera/%s" % config['percona']['cluster_name']
    ETCD_HOST = "etcd.%s.svc.%s" % (config['namespace'],
                                    config['cluster_domain'])
    ETCD_PORT = int(config['etcd']['client_port']['cont'])
    ETCD_TLS = config['etcd']['tls']['enabled']
    DHPARAM_CERT = config['security']['tls']['dhparam']


def get_mysql_client(insecure=False):

    password = '' if insecure else MYSQL_ROOT_PASSWORD
    return pymysql.connect(unix_socket='/var/run/mysqld/mysqld.sock',
                           user='root',
                           password=password,
                           connect_timeout=1,
                           read_timeout=1,
                           cursorclass=pymysql.cursors.DictCursor)


def get_etcd_client():

    if ETCD_TLS:
        protocol = 'https'
        ca_cert = CA_CERT
    else:
        protocol = 'http'
        ca_cert = None

    return etcd.Client(host=ETCD_HOST,
                       port=ETCD_PORT,
                       allow_reconnect=True,
                       protocol=protocol,
                       ca_cert=ca_cert,
                       read_timeout=2)


def datadir_cleanup(path):

    for filename in os.listdir(path):
        fullpath = os.path.join(path, filename)
        if os.path.isdir(fullpath):
            shutil.rmtree(fullpath)
        else:
            os.remove(fullpath)


def create_dhparam():
    if not os.path.isfile(DHPARAM):
        with open(DHPARAM, 'w') as f:
            f.write(DHPARAM_CERT)
            LOG.info("dhparam cert created in %s", DHPARAM)
    else:
        LOG.info("%s exists, not overriding it", DHPARAM)


def create_init_flag():

    if not os.path.isfile(INIT_FILE):
        open(INIT_FILE, 'a').close()
        LOG.debug("Create init_ok file: %s", INIT_FILE)
    else:
        LOG.debug("Init file: '%s' already exists", INIT_FILE)


def run_cmd(cmd, check_result=False):

    LOG.debug("Executing cmd:\n%s", cmd)
    proc = subprocess.Popen(cmd, shell=True)
    if check_result:
        proc.communicate()
        if proc.returncode != 0:
            raise ProcessException(proc.returncode)
    return proc


def run_mysqld(available_nodes, donors_list, etcd_client, lock):

    create_dhparam()
    cmd = ("mysqld --user=mysql --wsrep_cluster_name=%s"
           " --wsrep_cluster_address=%s"
           " --wsrep_sst_method=xtrabackup-v2"
           " --wsrep_sst_donor=%s"
           " --wsrep_node_address=%s"
           " --wsrep_node_name=%s"
           " --pxc_strict_mode=PERMISSIVE" %
           (six.moves.shlex_quote(CLUSTER_NAME),
            "gcomm://%s" % six.moves.shlex_quote(available_nodes),
            six.moves.shlex_quote(donors_list),
            six.moves.shlex_quote(IPADDR),
            six.moves.shlex_quote(IPADDR)))
    mysqld_proc = run_cmd(cmd)
    wait_for_mysqld_to_start(mysqld_proc, insecure=False)

    def sig_handler(signum, frame):
        LOG.info("Caught a signal: %d", signum)
        etcd_deregister_in_path(etcd_client, 'queue')
        etcd_deregister_in_path(etcd_client, 'nodes')
        etcd_deregister_in_path(etcd_client, 'seqno')
        etcd_delete_if_exists(etcd_client, 'leader', IPADDR)
        release_lock(lock)
        mysqld_proc.send_signal(signum)

    signal.signal(signal.SIGTERM, sig_handler)
    return mysqld_proc


def mysql_exec(mysql_client, sql_list):

    with mysql_client.cursor() as cursor:
        for cmd, args in sql_list:
            LOG.debug("Executing mysql cmd: %s\nWith the following args: '%s'",
                      cmd, args)
            cursor.execute(cmd, args)
        return cursor.fetchall()


@retry
def fetch_status(etcd_client, path):

    key = os.path.join(ETCD_PATH, path)
    try:
        root = etcd_client.get(key)
    except etcd.EtcdKeyNotFound:
        LOG.debug("Current nodes in %s is: %s", key, None)
        return []

    result = [str(child.key).replace(key + "/", '')
              for child in root.children
              if str(child.key) != key]
    LOG.debug("Current nodes in %s is: %s", key, result)
    return result


def fetch_wsrep_data():

    wsrep_data = {}
    mysql_client = get_mysql_client()
    data = mysql_exec(mysql_client, [("SHOW STATUS LIKE 'wsrep%'", None)])
    for i in data:
        wsrep_data[i['Variable_name']] = i['Value']
    return wsrep_data


@retry
def get_oldest_node_by_seqno(etcd_client, path):

    """
    This fucntion returns IP addr of the node with the highes seqno.

    seqno(sequence number) indicates the number of transactions ran thought
    that node. Node with highes seqno is the node with the lates data.

    """
    key = os.path.join(ETCD_PATH, path)
    root = etcd_client.get(key)
    # We need to cut etcd path prefix like "/galera/k8scluster/seqno/" to get
    # the IP addr of the node.
    prefix = key + "/"
    result = sorted([(str(child.key).replace(prefix, ''), int(child.value))
                     for child in root.children])
    result.sort(key=lambda x: x[1])
    LOG.debug("ALL seqno is %s", result)
    LOG.info("Oldest node is %s, am %s", result[-1][0], IPADDR)
    return result[-1][0]


@retry
def _etcd_set(etcd_client, path, value, ttl):

    key = os.path.join(ETCD_PATH, path)
    etcd_client.set(key, value, ttl=ttl)
    LOG.info("Set %s with value '%s' and ttl '%s'", key, value, ttl)


def _etcd_read(etcd_client, path):

    key = os.path.join(ETCD_PATH, path)
    try:
        value = etcd_client.read(key).value
        return value
    except etcd.EtcdKeyNotFound:
        return None


def etcd_register_in_path(etcd_client, path, ttl=60):

    key = os.path.join(path, IPADDR)
    _etcd_set(etcd_client, key, time.time(), ttl)


def etcd_set_seqno(etcd_client, ttl):

    seqno = mysql_get_seqno()
    key = os.path.join('seqno', IPADDR)
    _etcd_set(etcd_client, key, seqno, ttl)


def etcd_delete_if_exists(etcd_client, path, prevValue):

    key = os.path.join(ETCD_PATH, path)
    try:
        etcd_client.delete(key, prevValue=prevValue)
        LOG.warning("Deleted key %s, with previous value '%s'", key, prevValue)
    except etcd.EtcdKeyNotFound:
        LOG.warning("Key %s not exist", key)
    except etcd.EtcdCompareFailed:
        LOG.debug("Previous of the '%s' is not the '%s'", key, prevValue)


def etcd_deregister_in_path(etcd_client, path):

    key = os.path.join(ETCD_PATH, path, IPADDR)
    try:
        etcd_client.delete(key, recursive=True)
        LOG.warning("Deleted key %s", key)
    except etcd.EtcdKeyNotFound:
        LOG.warning("Key %s not exist", key)


def mysql_get_seqno():

    if os.path.isfile(GRASTATE_FILE):
        with open(GRASTATE_FILE) as f:
            content = f.readlines()
        for line in content:
            if line.startswith('seqno'):
                return line.partition(':')[2].strip()
    else:
        LOG.warning("Can't find a '%s' file. Setting seqno to '-1'",
                    GRASTATE_FILE)
        return -1


def check_for_stale_seqno(etcd_client):

    queue_set = set(fetch_status(etcd_client, 'queue'))
    seqno_set = set(fetch_status(etcd_client, 'seqno'))
    difference = queue_set - seqno_set
    if difference:
        LOG.warning("Found stale seqno entries: %s, deleting", difference)
        for ip in difference:
            key = os.path.join(ETCD_PATH, 'seqno', ip)
            try:
                etcd_client.delete(key)
                LOG.warning("Deleted key %s", key)
            except etcd.EtcdKeyNotFound:
                LOG.warning("Key %s not exist", key)
    else:
        LOG.debug("Found seqno set is equals to the queue set: %s = %s",
                  queue_set, seqno_set)


def check_if_sst_running():

    return os.path.isfile(SST_FLAG)


def wait_for_expected_state(etcd_client, ttl):

    while True:
        status = fetch_status(etcd_client, 'queue')
        if len(status) > EXPECTED_NODES:
            LOG.debug("Current number of nodes is %s, expected: %s, sleeping",
                      len(status), EXPECTED_NODES)
            time.sleep(10)
        elif len(status) < EXPECTED_NODES:
            LOG.debug("Current number of nodes is %s, expected: %s, sleeping",
                      len(status), EXPECTED_NODES)
            time.sleep(1)
        else:
            wait_for_my_turn(etcd_client)
            break


def wait_for_my_seqno(etcd_client):

        oldest_node = get_oldest_node_by_seqno(etcd_client, 'seqno')
        if IPADDR == oldest_node:
            LOG.info("It's my turn to join the cluster")
            return
        else:
            time.sleep(5)


def wait_for_my_turn(etcd_client):

    check_for_stale_seqno(etcd_client)
    LOG.info("Waiting for my turn to join cluster")
    if FORCE_BOOTSTRAP:
        LOG.warning("Force bootstrap flag was detected, skiping normal"
                    " bootstrap procedure")
        if FORCE_BOOTSTRAP_NODE is None:
            LOG.error("Force bootstrap node wasn't set. Can't continue")
            sys.exit(1)

        LOG.debug("Force bootstrap node is %s", FORCE_BOOTSTRAP_NODE)
        my_node_name = os.environ['CCP_NODE_NAME']
        if my_node_name == FORCE_BOOTSTRAP_NODE:
            LOG.info("This node is the force boostrap one.")
            set_safe_to_bootstrap()
            return
        else:
            LOG.info("This node is not the force boostrap one."
                     " Waiting for the bootstrap one to create a cluster.")
            while True:
                nodes = fetch_status(etcd_client, 'nodes')
                if nodes:
                    wait_for_my_seqno(etcd_client)
                    return
                else:
                    time.sleep(5)
    else:
        wait_for_my_seqno(etcd_client)


def wait_for_sync(mysqld):

    while True:
        try:
            wsrep_data = fetch_wsrep_data()
            state = int(wsrep_data['wsrep_local_state'])
            if state == 4:
                LOG.info("Node synced")
                # If sync was done by SST all files in datadir was lost
                create_init_flag()
                break
            else:
                LOG.debug("Waiting node to be synced. Current state is: %s",
                          wsrep_data['wsrep_local_state_comment'])
                time.sleep(5)
        except Exception:
            if mysqld.poll() is None:
                time.sleep(5)
            else:
                LOG.error('Mysqld was terminated, exit code was: %s',
                          mysqld.returncode)
                sys.exit(mysqld.returncode)


def check_if_im_last(etcd_client):

    sleep = 10
    queue_status = fetch_status(etcd_client, 'queue')
    while True:
        nodes_status = fetch_status(etcd_client, 'nodes')
        if len(nodes_status) > EXPECTED_NODES:
            LOG.info("Looks like we have stale data in etcd, found %s nodes, "
                     "but expected to find %s, sleeping for %s sec",
                     len(nodes_status), EXPECTED_NODES, sleep)
            time.sleep(sleep)
        else:
            break
    if not queue_status and len(nodes_status) == EXPECTED_NODES:
        LOG.info("Looks like this node is the last one")
        return True
    else:
        LOG.info("I'm not the last node")
        return False


def create_join_list(status, leader, donor=False):

    if IPADDR in status:
        status.remove(IPADDR)
    if leader in status and donor:
        status.remove(leader)

    if not status:
        if donor:
            LOG.info("No available nodes found. Using empty donor list")
            return (",")
        else:
            LOG.info("No available nodes found. Assuming I'm first")
            return ("", True)
    else:
        if donor:
            # We need to keep trailing comma at the end
            donor_list = "%s," % ','.join(status)
            LOG.debug("Donor list is: '%s'", donor_list)
            return donor_list
        else:
            LOG.info("Joining to nodes %s", ','.join(status))
            return (','.join(status), False)


def update_uuid(etcd_client):

    wsrep_data = fetch_wsrep_data()
    uuid = wsrep_data['wsrep_cluster_state_uuid']
    _etcd_set(etcd_client, 'uuid', uuid, ttl=None)


def update_cluster_state(etcd_client, state):

    _etcd_set(etcd_client, 'state', state, ttl=None)


def wait_for_mysqld(proc):

    code = proc.wait()
    LOG.info("Process exited with code %d", code)
    sys.exit(code)


def wait_for_mysqld_to_start(proc, insecure):

    LOG.info("Waiting mysql to start...")
    # Sometimes initial mysql start could take some time, especialy with SSL
    # enabled. FIXME - replace sleep with some additional checks.
    time.sleep(30)
    while True:
        if check_if_sst_running():
            LOG.debug("SST sync detected, waiting...")
            time.sleep(30)
        else:
            LOG.debug("No SST sync detected")
            break

    for i in range(0, 59):
        try:
            mysql_client = get_mysql_client(insecure=insecure)
            mysql_exec(mysql_client, [("SELECT 1", None)])
            return
        except Exception:
            time.sleep(1)
    else:
        LOG.error("Mysql boot failed")
        raise RuntimeError("Process exited with code: %s" % proc.returncode)


def wait_for_mysqld_to_stop():

    """
    Since mysqld start wrapper first, we can't check for the executed proc
    exit code and be assured that mysqld itself is finished working. We have
    to check whole process group, so we're going to use pgrep for this.
    """

    LOG.info("Waiting for mysqld to finish working")
    for i in range(0, 29):
        proc = run_cmd("pgrep mysqld")
        proc.communicate()
        if proc.returncode == 0:
            time.sleep(1)
        else:
            LOG.info("Mysqld finished working")
            break
    else:
        LOG.info("Can't kill the mysqld process used for bootstraping")
        sys.exit(1)


def mysql_init():
    datadir_cleanup(DATADIR)
    run_cmd("mysqld --initialize-insecure", check_result=True)
    mysqld_proc = run_cmd("mysqld --skip-networking")
    wait_for_mysqld_to_start(mysqld_proc, insecure=True)

    LOG.info("Mysql is running, setting up the permissions")
    sql_list = [("CREATE USER 'root'@'%%' IDENTIFIED BY %s",
                MYSQL_ROOT_PASSWORD),
                ("GRANT ALL ON *.* TO 'root'@'%' WITH GRANT OPTION", None),
                ("ALTER USER 'root'@'localhost' IDENTIFIED BY %s",
                MYSQL_ROOT_PASSWORD),
                ("CREATE USER 'xtrabackup'@'localhost' IDENTIFIED BY %s",
                XTRABACKUP_PASSWORD),
                ("GRANT RELOAD,PROCESS,LOCK TABLES,REPLICATION CLIENT ON *.*"
                " TO 'xtrabackup'@'localhost'", None),
                ("GRANT REPLICATION CLIENT ON *.* TO monitor@'%%' IDENTIFIED"
                " BY %s", MONITOR_PASSWORD),
                ("DROP DATABASE IF EXISTS test", None),
                ("FLUSH PRIVILEGES", None)]
    try:
        mysql_client = get_mysql_client(insecure=True)
        mysql_exec(mysql_client, sql_list)
    except Exception:
        raise

    create_init_flag()
    # It's more safe to kill mysqld via pkill, since mysqld start wrapper first
    run_cmd("pkill mysqld")
    wait_for_mysqld_to_stop()
    LOG.info("Mysql bootstraping is done")


def check_cluster(etcd_client):

    state = _etcd_read(etcd_client, 'state')
    nodes_status = fetch_status(etcd_client, 'nodes')
    if not nodes_status and state == 'STEADY':
        LOG.warning("Cluster is in the STEADY state, but there no"
                    " alive nodes detected, running cluster recovery")
        update_cluster_state(etcd_client, 'RECOVERY')


def acquire_lock(lock, ttl):

    LOG.info("Locking...")
    lock.acquire(blocking=True, lock_ttl=ttl)
    LOG.info("Successfuly acquired lock")


def release_lock(lock):

    lock.release()
    LOG.info("Successfuly released lock")


def set_safe_to_bootstrap():

    """
    Less wordy way to do "inplace" edit of the file
    """

    for line in fileinput.input(GRASTATE_FILE, inplace=1):
        if line.startswith("safe_to_bootstrap"):
            line = line.replace("safe_to_bootstrap: 0", "safe_to_bootstrap: 1")
            sys.stdout.write(line)


def run_create_queue(etcd_client, lock, ttl):

    """
    In this step we're making recovery preparations.

    We need to get our seqno from mysql, after that we done, we'll fall into
    the endless loop waiting 'till other nodes do the same and after that we
    wait for our turn, based on the seqno, to start jointing the cluster.
    """

    LOG.info("Creating recovery queue")
    etcd_register_in_path(etcd_client, 'queue', ttl=None)
    etcd_set_seqno(etcd_client, ttl=None)
    release_lock(lock)
    wait_for_expected_state(etcd_client, ttl)


def run_join_cluster(etcd_client, lock, ttl):

    """
    In this step we're ready to join or create new cluster.

    We get current nodes list, and it's empty it means we're the first one.
    If the seqno queue list is empty and nodes list is equals to 3, we assume
    that we're the last one. In the one last case we're the second one.

    If we're the first one, we're creating the new cluster.
    If we're the second one or last one, we're joinning to the existing
    cluster.

    If cluster state was a RECOVERY we do the same thing, but nodes take turns
    not by first come - first served rule, but by the seqno of their data, so
    first one node will the one with the most recent data.
    """

    LOG.info("Joining the cluster")
    acquire_lock(lock, ttl)
    state = _etcd_read(etcd_client, 'state')
    nodes_status = fetch_status(etcd_client, 'nodes')
    leader = _etcd_read(etcd_client, 'leader')
    available_nodes, first_one = create_join_list(nodes_status, leader)
    donors_list = create_join_list(nodes_status, leader, donor=True)
    if first_one:
        set_safe_to_bootstrap()
        # First node shouldn't have a TTL during the cluster bootstrap
        ttl = None
    mysqld = run_mysqld(available_nodes, donors_list, etcd_client, lock)
    wait_for_sync(mysqld)
    etcd_register_in_path(etcd_client, 'nodes', ttl)
    if state == "RECOVERY":
        etcd_deregister_in_path(etcd_client, 'seqno')
        etcd_deregister_in_path(etcd_client, 'queue')
    last_one = check_if_im_last(etcd_client)
    release_lock(lock)
    return (first_one, last_one, mysqld)


def run_update_metadata(etcd_client, first_one, last_one):

    """
    In this step we updating the cluster state and metadata.

    If node was the first one, it change the state of the cluster to the
    BUILDING and sets it's uuid as a cluster uuid in etcd.

    If node was the last one it change the state of the cluster to the STEADY.

    Please note, that if it was a RECOVERY scenario, we dont change state of
    the cluster until it will be fully rebuilded.
    """

    LOG.info("Update cluster metadata")
    state = _etcd_read(etcd_client, 'state')
    if first_one:
        update_uuid(etcd_client)
        if state != 'RECOVERY':
            update_cluster_state(etcd_client, 'BUILDING')
    if last_one:
        update_cluster_state(etcd_client, 'STEADY')


def main(ttl):

    if not os.path.isfile(INIT_FILE):
        LOG.info("Init file '%s' not found, doing full init", INIT_FILE)
        mysql_init()
    else:
        LOG.info("Init file '%s' found. Skiping mysql bootstrap and run"
                 " wsrep-recover", INIT_FILE)
        run_cmd("mysqld_safe --wsrep-recover", check_result=True)

    try:
        LOG.debug("My IP is: %s", IPADDR)
        etcd_client = get_etcd_client()
        lock = etcd.Lock(etcd_client, 'galera')
        acquire_lock(lock, ttl)
        check_cluster(etcd_client)
        state = _etcd_read(etcd_client, 'state')

        # Scenario 1: Initial bootstrap
        if state is None or state == 'BUILDING':
            LOG.info("No running cluster detected - starting bootstrap")
            first_one, last_one, mysqld = run_join_cluster(etcd_client, lock,
                                                           ttl)
            run_update_metadata(etcd_client, first_one, last_one)
            LOG.info("Bootsraping is done. Node is ready.")

        # Scenario 2: Re-connect
        elif state == 'STEADY':
            LOG.info("Detected running cluster, re-connecting")
            first_one, last_one, mysqld = run_join_cluster(etcd_client, lock,
                                                           ttl)
            LOG.info("Node joined and ready")

        # Scenario 3: Recovery
        elif state == 'RECOVERY':
            LOG.warning("Cluster is in the RECOVERY state, re-connecting to"
                        " the node with the oldest data")
            run_create_queue(etcd_client, lock, ttl)
            first_one, last_one, mysqld = run_join_cluster(etcd_client, lock,
                                                           ttl)
            run_update_metadata(etcd_client, first_one, last_one)
            LOG.info("Recovery is done. Node is ready.")

        wait_for_mysqld(mysqld)
    except Exception as err:
        LOG.exception(err)
        raise
    finally:
        etcd_deregister_in_path(etcd_client, 'queue')
        etcd_deregister_in_path(etcd_client, 'nodes')
        etcd_deregister_in_path(etcd_client, 'seqno')
        etcd_delete_if_exists(etcd_client, 'leader', IPADDR)
        release_lock(lock)


if __name__ == "__main__":
    get_config()
    set_globals()
    main(ttl=300)

# vim: set ts=4 sw=4 tw=0 et :
