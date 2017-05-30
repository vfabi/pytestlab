import time
import threading
import os
import posixpath
import socket
import pytest
import etcd
import logging
from collections import namedtuple, OrderedDict, defaultdict
from dns import rdatatype, resolver


SRV = namedtuple('SRV', ['host', 'port', 'priority', 'weight'])

logger = logging.getLogger(__name__)


class ResourceLocked(Exception):
    """Attempt to lock a resource already locked by an external test session.
    """


class TooManyLocks(Exception):
    """This resource has already been locked by the current test session."""


class SRVQueryFailure(Exception):
    """Exception that is raised when the DNS query has failed."""
    def __str__(self):
        return 'SRV query failure: %s' % self.args[0]


def _build_resource_to_address_map(answer):
    """Return a dictionary that maps resource name to address.
    The response from any DNS query is a list of answer records and
    a list of additional records that may be useful.  In the case of
    SRV queries, the answer section contains SRV records which contain
    the service weighting information and a DNS resource name which
    requires further resolution.  The additional records segment may
    contain A records for the resources.  This function collects them
    into a dictionary that maps resource name to an array of addresses.
    :rtype: dict
    """
    mapping = defaultdict(list)
    for resource in answer.response.additional:
        target = resource.name.to_text()
        mapping[target].extend(record.address
                               for record in resource.items
                               if record.rdtype == rdatatype.A)
    return mapping


def _build_result_set(answer):
    """Return a list of SRV instances for a DNS answer.
    :rtype: list of srvlookup.SRV
    """
    resource_map = _build_resource_to_address_map(answer)
    for resource in answer:
        target = resource.target.to_text()
        if target in resource_map:
            for address in resource_map[target]:
                yield SRV(address, resource.port, resource.priority,
                          resource.weight)
        else:
            yield SRV(target.rstrip('.'), resource.port, resource.priority,
                      resource.weight)


def query_etcd_server(discovery_srv):
    fqdn = '.'.join(('_etcd-server', '_tcp', discovery_srv))

    try:
        answer = resolver.query(fqdn, 'SRV')
    except (resolver.NoAnswer,
            resolver.NoNameservers,
            resolver.NotAbsolute,
            resolver.NoRootSOA,
            resolver.NXDOMAIN) as error:
        raise SRVQueryFailure(error.__class__.__name__)

    results = _build_result_set(answer)
    return sorted(results, key=lambda r: (r.priority, -r.weight, r.host))


def connect_to_etcd(discovery_srv):
    err = None
    for record in query_etcd_server(discovery_srv):
        try:
            return etcd.Client(host=record.host, port=2379)
        except Exception as _:
            err = _

    if err:
        raise err
    raise RuntimeError("What happened?")


def _makekey(name):
    return posixpath.join('lab', 'locks', name)


class Lock(object):
    def __init__(self, key, **kwargs):
        self.key = _makekey(key)
        self.data = kwargs


class EtcdLocker(object):
    def __init__(self, discovery_srv):
        self.etcd = connect_to_etcd(discovery_srv)
        self.locks = {}

    def read(self, key):
        try:
            return self.etcd.read(_makekey(key))
        except etcd.EtcdKeyNotFound:
            return None

    def write(self, key, lock, ttl):
        lock = Lock(key, locker=lock)
        self.etcd.write(lock.key, lock.data, ttl=ttl, prevexists=False)
        self.locks[key] = lock

    def refresh(self, key, ttl):
        lock = self.locks[key]
        self.etcd.write(lock.key, lock.data, ttl=ttl, refresh=True)

    def release(self, key):
        lock = self.locks.pop(key)
        try:
            self.etcd.delete(lock.key)
        except etcd.EtcdKeyNotFound:
            pass

    def test(self, name):
        return _makekey(name) in self.locks


def get_lock_id(user=None):
    return '@'.join((user or os.environ.get("USER", "anonymous"),
                     socket.getfqdn()))


class Locker(object):
    def __init__(self, config, backend):
        self.config = config
        self.backend = backend

        self.ttl = 30  # XXX: FIX

        self._stop = threading.Event()
        self._thread = None

    def aquire(self, key, user=None):
        record = self.backend.read(key)

        if record and record.ttl:
            print("MESSAGE TTL={}".format(record.ttl))
            logger.error('{} is locked by {}, waiting {} seconds for lock '
                         'to expire...'.format(key, record.value, record.ttl + 1))
            start = time.time()
            while time.time() - start < record.ttl + 1:
                record = self.backend.read(key)
                if not record:
                    break
                time.sleep(0.5)
        elif record:
            raise ResourceLocked(
                '{} is currently locked by {}'.format(key, record.value))

        # acquire
        lockid = get_lock_id(user)
        logger.info("{} is acquiring lock for {}".format(lockid, key))
        self.backend.write(key, lockid, self.ttl)
        logger.debug("Locked {}:{}".format(key, lockid))

        # start keep-alive
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._keepalive)
            self._thread.start()

        return key, lockid

    def release_all(self):
        self._stop.set()
        for key in list(self.backend.locks):
            self.backend.release(key)

    def _keepalive(self):
        logger.critical("Starting keep-alive thread...")
        while not self._stop.wait(self.ttl // 2):
            for key in list(self.backend.locks):
                logger.warning("Relocking {}".format(key))
                self.backend.refresh(key, self.ttl)

    @pytest.hookimpl
    def pytest_unconfigure(self, config):
        self.release_all()

    @pytest.hookimpl
    def pytest_lab_lock(self, config, identifier):
        pytest.log.info("ATTEMPTING TO LOCK {}".format(identifier))
        self.aquire(identifier)
        return True


@pytest.hookimpl
def pytest_configure(config):
    etcd = EtcdLocker('qa.sangoma.local')
    config.pluginmanager.register(Locker(config, etcd))
