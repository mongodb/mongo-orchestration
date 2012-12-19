#!/usr/bin/python
# coding=utf-8

import socket
import subprocess
import time
import os
import errno
from singleton import Singleton
import tempfile
import shutil
import stat
import json

import logging
logger = logging.getLogger(__name__)

HOME = os.environ.get('HOME')
HOSTNAME = os.environ.get('HOSTNAME', socket.gethostname())
HOSTNAME = socket.gethostbyname_ex(HOSTNAME)[2][0]
HOSTNAME = 'localhost'


class PortPool(Singleton):

    __ports = set()
    __closed = set()
    __id = None

    def __init__(self, min_port=1025, max_port=2000, port_sequence=None):
        """
        Args:
            min_port - min port number  (ignoring if 'port_sequence' is not None)
            max_port - max port number  (ignoring if 'port_sequence' is not None)
            port_sequence - iterate sequence which contains numbers of ports
        """
        if not self.__id:  # singleton checker
            self.__id = id(self)
            self.__init_range(min_port, max_port, port_sequence)

    def __init_range(self, min_port=1025, max_port=2000, port_sequence=None):
        if port_sequence:
            self.__ports = set(port_sequence)
        else:
            self.__ports = set(xrange(min_port, max_port + 1))
        self.__closed = set()
        self.refresh()

    def __check_port(self, port):
        """check port status
        return True if port is free, False else
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((HOSTNAME, port))
            return True
        except socket.error:
            return False
        finally:
            s.close()

    def release_port(self, port):
        """release port"""
        if port in self.__closed:
            self.__closed.remove(port)
        self.__ports.add(port)

    def port(self, check=False):
        """return next opened port
        Args:
          check - check is port realy free
        """
        if not self.__ports:  # refresh ports if sequence is empty
            self.refresh()

        try:
            port = self.__ports.pop()
            if check:
                while not self.__check_port(port):
                    self.release_port(port)
                    port = self.__ports.pop()
        except (IndexError, KeyError):
            raise IndexError("Could not find a free port,\nclosed ports: {closed}".format(closed=self.__closed))
        self.__closed.add(port)
        return port

    def refresh(self, only_closed=False):
        """refresh ports status
        Args:
          only_closed - check status only for closed ports
        """
        if only_closed:
            opened = filter(self.__check_port, self.__closed)
            self.__closed = self.__closed.difference(opened)
            self.__ports = self.__ports.union(opened)
        else:
            ports = self.__closed.union(self.__ports)
            self.__ports = set(filter(self.__check_port, ports))
            self.__closed = ports.difference(self.__ports)

    def change_range(self, min_port=1025, max_port=2000, port_sequence=None):
        """change Pool port range"""
        self.__init_range(min_port, max_port, port_sequence)


def wait_for(port_num, timeout):
    """waits while process starts.
    Args:
        port_num    - port number
        timeout     - specify how long, in seconds, a command can take before times out.
    return True if process started, return False if not
    """
    t_start = time.time()
    sleeps = 1
    while time.time() - t_start < timeout:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect((HOSTNAME, port_num))
                return True
            except (IOError, socket.error):
                time.sleep(sleeps)
        finally:
            s.close()
    return False


def mprocess(name, config_path, port=None, timeout=180):
    """start 'name' process with params from config_path.
    Args:
        name - process name or path
        config_path - path to file where should be stored configuration
        port - process's port
        timeout - specify how long, in seconds, a command can take before times out.
                  if timeout <=0 - doesn't wait for complete start process
    return tuple (pid, host) if process started, return (None, None) if not
    """
    logger.debug("mprocess({name}, {config_path}, {port}, {timeout})".format(**locals()))
    port = port or PortPool().port(check=True)
    cmd = [name, "--config", config_path]
    host = HOSTNAME + str(port)
    try:
        logger.debug("execute process: {cmd}".format(**locals()))
        proc = subprocess.Popen(cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        if not proc_alive(proc.pid):
            raise OSError
    except (OSError, TypeError) as err:
        logger.debug("exception while executing process: {err}".format(**locals()))
        raise OSError
    if timeout > 0 and wait_for(port, timeout):
        logger.debug("process '{name}' has started: pid={proc.pid}, host={host}".format(**locals()))
        return (proc.pid, host)
    elif timeout > 0:
        logger.debug("hasn't connected to pid={proc.pid} with host={host} during timeout {timeout} ".format(**locals()))
        logger.debug("terminate process with pid={proc.pid}".format(**locals()))
        proc.terminate()
        proc_alive(proc.pid) and time.sleep(3)  # wait while process stoped
        raise OSError(errno.ETIMEDOUT, "could not connect to process during {timeout} seconds".format(timeout=timeout))
    return (proc.pid, host)


def kill_mprocess(pid, timeout=10):
    """kill process
    Args:
        pid - process pid
    """
    if pid and proc_alive(pid):
        os.kill(pid, 15)
        try:
            os.wait()
        except OSError:
            pass
        t_start = time.time()
        while proc_alive(pid) and time.time() - t_start < timeout:
            time.sleep(1.5)
    return not proc_alive(pid)


def cleanup_mprocess(config_path, cfg):
    """remove all process's stuff
    Args:
       config_path - process's options file
       cfg - process's config
    """
    for key in ('keyFile', 'logPath', 'dbpath'):
        remove_path(cfg.get(key, None))
    remove_path(config_path)


def remove_path(path):
    """remove path from file system
    If path is None - do nothing"""
    onerror = lambda func, filepath, exc_info: (time.sleep(2), os.chmod(filepath, stat.S_IWUSR), func(filepath))
    if path is None or not os.path.exists(path):
        return
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=onerror)
    if os.path.isfile(path):
        try:
            shutil.os.remove(path)
        except OSError:
            onerror(shutil.os.remove, path, None)


def write_config(params, config_path=None):
    """write mongo*'s config file
    Args:
       params - options wich file contains
       config_path - path to the config_file, will create if None
    Return config_path
       where config_path - path to mongo*'s options file
    """
    if config_path is None:
        config_path = tempfile.mktemp(prefix="mongo-")

    cfg = params.copy()
    # fix boolean value
    for key, value in cfg.items():
        if isinstance(value, bool):
            cfg[key] = json.dumps(value)

    with open(config_path, 'w') as fd:
        data = reduce(lambda s, item: "{s}\n{key}={value}".format(s=s, key=item[0], value=item[1]), cfg.items(), '')
        fd.write(data)
    return config_path


def proc_alive(pid):
    """check if process with pid is alive
    Return True or False"""

    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False
