import logging
import gzip
import os
import signal
from stat import ST_SIZE, ST_INO
import time
import subprocess
try:
    import bz2
    HAS_BZ2 = True
except ImportError:
    HAS_BZ2 = False

from .allowedhosts import AllowedHosts
from .constants import *
from .daemon import createDaemon
from .denyfileutil import Purge
from .filetracker import FileTracker
from .loginattempt import LoginAttempt
from . import plugin
from .regex import *
from .report import Report
from .restricted import Restricted
from .sync import Sync
from .util import die, is_true, parse_host, send_email, is_valid_ip_address
from .version import VERSION

debug = logging.getLogger("denyhosts").debug
info = logging.getLogger("denyhosts").info
error = logging.getLogger("denyhosts").error
warning = logging.getLogger("denyhosts").warning


def system_execute(args, valid_retcodes = [0], use_stdout = True, use_stderr = True):
    try:
        if use_stderr and use_stdout:
            retcode = subprocess.call(args)
        else:
            with open(os.devnull, "w") as fnull:
                params = {}
                if not use_stdout:
                    params["stdout"] = fnull
                if not use_stderr:
                    params["stderr"] = fnull
                retcode = subprocess.call(args, **params)
        if not retcode in valid_retcodes:
            raise RuntimeError("Return code %d" % retcode)
        return retcode
    except Exception as e:
        error("Failure to execute %s" % " ".join(args))
        error(str(e))
        raise


class DenyHosts(object):
    def __init__(self, logfile, prefs, lock_file,
                 ignore_offset=0, first_time=0,
                 noemail=0, daemon=0, foreground=0):
        self.__denied_hosts = {}
        self.__prefs = prefs
        self.__lock_file = lock_file
        self.__first_time = first_time
        self.__noemail = noemail
        self.__report = Report(prefs.get("HOSTNAME_LOOKUP"), is_true(prefs['SYSLOG_REPORT']))
        self.__daemon = daemon
        self.__foreground = foreground
        self.__sync_server = prefs.get('SYNC_SERVER')
        self.__sync_upload = is_true(prefs.get("SYNC_UPLOAD"))
        self.__sync_download = is_true(prefs.get("SYNC_DOWNLOAD"))
        self.__iptables = prefs.get("IPTABLES")
        self.__ipset_cmd = prefs.get("IPSET_COMMAND")
        self.__ipset_name = prefs.get("IPSET_NAME")
        self.__blockport = prefs.get("BLOCKPORT")
        self.__pfctl = prefs.get("PFCTL_PATH")
        self.__pftable = prefs.get("PF_TABLE")
        self.__pftablefile = prefs.get("PF_TABLE_FILE")
        self.__fw_initialized = False
        self.__fw_blocked_hosts = set()

        r = Restricted(prefs)
        self.__restricted = r.get_restricted()
        info("restricted: %s", self.__restricted)
        self.init_regex()

        try:
            self.file_tracker = FileTracker(self.__prefs.get('WORK_DIR'),
                                            logfile)
        except Exception as e:
            self.__lock_file.remove()
            die("Can't read: %s" % logfile, e)

        self.__allowed_hosts = AllowedHosts(self.__prefs)

        if ignore_offset:
            last_offset = 0
        else:
            last_offset = self.file_tracker.get_offset()


        if last_offset is not None:
            self.get_denied_hosts()
            info("Processing log file (%s) from offset (%ld)",
                 logfile,
                 last_offset)
            offset = self.process_log(logfile, last_offset)
            if offset != last_offset:
                self.file_tracker.save_offset(offset)
                last_offset = offset
        elif not daemon:
            info("Log file size has not changed.  Nothing to do.")

        if daemon and not foreground:
            info("launching DenyHosts daemon (version %s)..." % VERSION)

            #logging.getLogger().setLevel(logging.WARN)

            # remove lock file since createDaemon will
            # create a new pid.  A new lock
            # will be created when runDaemon is invoked
            self.__lock_file.remove()

            retCode = createDaemon()
            if retCode == 0:
                self.runDaemon(logfile, last_offset)
            else:
                die("Error creating daemon: %s (%d)" % (retCode[1], retCode[0]))
        elif foreground:
            info("launching DenyHost (version %s)..." % VERSION)
            self.__lock_file.remove()
            self.runDaemon(logfile, last_offset)


    def killDaemon(self, signum, frame):
        debug("Received SIGTERM")
        info("DenyHosts daemon is shutting down")
        # signal handler

        # self.__lock_file.remove()
        # lock will be freed on SIGTERM by denyhosts.py
        # exception handler (SystemExit)
        sys.exit(0)


    def toggleDebug(self, signum, frame):
        level = logging.getLogger().getEffectiveLevel()
        if level == logging.INFO:
            level = logging.DEBUG
            name = "DEBUG"
        else:
            level = logging.INFO
            name = "INFO"
        info("setting debug level to: %s", name)
        logging.getLogger().setLevel(level)


    def runDaemon(self, logfile, last_offset):
        #signal.signal(signal.SIGHUP, self.killDaemon)
        signal.signal(signal.SIGTERM, self.killDaemon)
        signal.signal(signal.SIGUSR1, self.toggleDebug)
        info("DenyHost daemon is now running, pid: %s", os.getpid())
        info("send daemon process a TERM signal to terminate cleanly")
        info("  eg.  kill -TERM %s", os.getpid())
        self.__lock_file.create()

        info("monitoring log: %s", logfile)
        daemon_sleep = self.__prefs.get('DAEMON_SLEEP')
        purge_time = self.__prefs.get('PURGE_DENY')
        sync_time = self.__prefs.get('SYNC_INTERVAL')
        info("sync_time: %s", str(sync_time))

        if purge_time:
            daemon_purge = self.__prefs.get('DAEMON_PURGE')
            daemon_purge = max(daemon_sleep, daemon_purge)
            purge_sleep_ratio = daemon_purge / daemon_sleep
            self.purge_counter = 0
            info("daemon_purge:      %ld", daemon_purge)
            info("daemon_sleep:      %ld", daemon_sleep)
            info("purge_sleep_ratio: %ld", purge_sleep_ratio)
        else:
            purge_sleep_ratio = None
            info("purging of %s is disabled", self.__prefs.get('HOSTS_DENY'))


        if sync_time and self.__sync_server:
            if sync_time < SYNC_MIN_INTERVAL:
                info("SYNC_INTERVAL (%d) should be atleast %d",
                     sync_time,
                     SYNC_MIN_INTERVAL)
                sync_time = SYNC_MIN_INTERVAL
            sync_time = max(daemon_sleep, sync_time)
            info("sync_time:      : %ld", sync_time)
            sync_sleep_ratio = sync_time / daemon_sleep
            self.sync_counter = 0
            info("sync_sleep_ratio: %ld", sync_sleep_ratio)
        else:
            sync_sleep_ratio = None
            info("denyhost synchronization disabled")

        self.daemonLoop(logfile, last_offset, daemon_sleep,
                        purge_time, purge_sleep_ratio, sync_sleep_ratio)


    def daemonLoop(self, logfile, last_offset, daemon_sleep,
                   purge_time, purge_sleep_ratio, sync_sleep_ratio):

        fp = open(logfile, "r")
        inode = os.fstat(fp.fileno())[ST_INO]

        while 1:

            try:
                curr_inode = os.stat(logfile)[ST_INO]
            except OSError:
                info("%s has been deleted", logfile)
                self.sleepAndPurge(daemon_sleep,
                                   purge_time,
                                   purge_sleep_ratio)
                continue

            if curr_inode != inode:
                info("%s has been rotated", logfile)
                inode = curr_inode
                try:
                    fp.close()
                except IOError:
                    pass

                fp = open(logfile, "r")
                # this ultimately forces offset (if not 0) to be < last_offset
                last_offset = sys.maxsize


            offset = os.fstat(fp.fileno())[ST_SIZE]
            if last_offset is None:
                last_offset = offset

            if offset > last_offset:
                # new data added to logfile
                debug("%s has additional data", logfile)

                self.get_denied_hosts()
                last_offset = self.process_log(logfile, last_offset)

                self.file_tracker.save_offset(last_offset)
            elif offset == 0:
                # log file rotated, nothing to do yet...
                # since there is no first_line
                debug("%s is empty.  File was rotated", logfile)
            elif offset < last_offset:
                # file was rotated or replaced and now has data
                debug("%s most likely rotated and now has data", logfile)
                last_offset = 0
                self.file_tracker.update_first_line()
                continue

            self.sleepAndPurge(daemon_sleep, purge_time,
                               purge_sleep_ratio, sync_sleep_ratio)



    def sleepAndPurge(self, sleep_time, purge_time,
                      purge_sleep_ratio = None, sync_sleep_ratio = None):
        time.sleep(sleep_time)
        if purge_time:
            self.purge_counter += 1
            if self.purge_counter == purge_sleep_ratio:
                try:
                    purge = Purge(self.__prefs,
                                  purge_time)
                except Exception as e:
                    logging.getLogger().exception(e)
                    raise
                self.purge_counter = 0

        if sync_sleep_ratio:
            #debug("sync count: %d", self.sync_counter)
            self.sync_counter += 1
            if self.sync_counter == sync_sleep_ratio:
                try:
                    sync = Sync(self.__prefs)
                    if self.__sync_upload:
                        debug("sync upload")
                        timestamp = sync.send_new_hosts()
                    if self.__sync_download:
                        debug("sync download")
                        new_hosts = sync.receive_new_hosts()
                        if new_hosts:
                            info("received new hosts: %s", str(new_hosts))
                            self.get_denied_hosts()
                            self.update_hosts_deny(new_hosts)
                    sync.xmlrpc_disconnect()
                except Exception as e:
                    logging.getLogger().exception(e)
                    raise
                self.sync_counter = 0


    def get_denied_hosts(self):
        self.__denied_hosts = {}
        for line in open(self.__prefs.get('HOSTS_DENY'), "r"):
            if line[0] not in ('#', '\n'):

                idx = line.find('#')
                if idx != 1:
                    line = line[:idx]
                try:
                    host = parse_host(line)
                    self.__denied_hosts[host] = 0
                    if host in self.__allowed_hosts:
                        self.__allowed_hosts.add_warned_host(host)
                except Exception:
                    pass

        new_warned_hosts = self.__allowed_hosts.get_new_warned_hosts()
        if new_warned_hosts:
            self.__allowed_hosts.save_warned_hosts()

            text = """WARNING: The following hosts appear in %s but should be
allowed based on your %s file"""  % (self.__prefs.get("HOSTS_DENY"),
                                     os.path.join(self.__prefs.get("WORK_DIR"),
                                                  ALLOWED_HOSTS))
            self.__report.add_section(text, new_warned_hosts)
            self.__allowed_hosts.clear_warned_hosts()


    def firewall_init(self):
        try:
            if self.__iptables and self.__ipset_name and self.__ipset_cmd:
                args = [self.__ipset_cmd, "list", self.__ipset_name, "-name"]
                debug("Checking if ipset exists: %s", " ".join(args))
                check_result = system_execute(args, [0,1], use_stdout=False)
                if check_result == 0:
                    debug("Ipset already exists")
                else:
                    args = [self.__ipset_cmd, "create", self.__ipset_name, "hash:ip", "-exist"]
                    debug("Creating an ipset: %s", " ".join(args))
                    system_execute(args)
                if self.__blockport:
                    match_rule = ["INPUT", "-p", "tcp", "--dport", self.__blockport, "-m", "set", "--match-set", self.__ipset_name, "src", "-j", "DROP"]
                else:
                    match_rule = ["INPUT", "-m", "set", "--match-set", self.__ipset_name, "src", "-j", "DROP"]
                args = [self.__iptables, "-C"] + match_rule
                debug("Checking if ipset match rule exists: %s", " ".join(args))
                check_result = system_execute(args, [0,1], False)  #check produces an error if ipset does not yet exist
                if check_result == 0:
                    debug("Ipset match rule already exists")
                else:
                    args = [self.__iptables, "-I"] + match_rule
                    debug("Creating ipset match rule: %s", " ".join(args))
                    system_execute(args)
            self.__fw_initialized = True
        except Exception as e:
            print(e)
            print("Unable to setup the firewall")


    def firewall_block(self, hosts):
        if self.__iptables:
            if self.__ipset_name and self.__ipset_cmd:
                self.firewall_init()  #we call this in case firewall rule or ipset were deleted
                debug("Adding host to the ipset")
                try:
                    for host in hosts:
                        my_host = str(host)
                        args = [self.__ipset_cmd, "add", self.__ipset_name, my_host, "-exist"]
                        debug("Adding host to the ipset: %s", " ".join(args))
                        info("Adding %s to ipset %s", my_host, self.__ipset_name)
                        system_execute(args)
                        self.__fw_blocked_hosts.add(host)
                except Exception as e:
                    print(e)
                    print("Unable to add a host to ipset")
            else:
                debug("Trying to create iptables rules")
                try:
                    for host in hosts:
                        my_host = str(host)
                        if self.__blockport:
                            args = [self.__iptables, "-I", "INPUT", "-p", "tcp", "--dport", self.__blockport, "-s", my_host, "-j", "DROP"]
                        else:
                            args = [self.__iptables, "-I", "INPUT", "-s", my_host, "-j", "DROP"]
                        cmd = " ".join(args)
                        debug("Adding iptables rule: %s", cmd)
                        info("Creating new firewall rule %s", cmd)
                        system_execute(args)
                        self.__fw_blocked_hosts.add(host)
                except Exception as e:
                    print(e)
                    print("Unable to write new firewall rule.")

        elif self.__pfctl and self.__pftable:
            debug("Trying to update PF table.")
            try:
                for host in hosts:
                    my_host = str(host)
                    args = [self.__pfctl, "-t", self.__pftable, "-T", "add", my_host]
                    cmd = " ".join(args)
                    debug("Running PF update rule: %s", cmd)
                    info("Creating new PF rule %s", cmd)
                    system_execute(args);
                    self.__fw_blocked_hosts.add(host)
            except Exception as e:
                print(e)
                print("Unable to write new PF rule.")

        if self.__pftablefile:
              debug("Trying to write host to PF table file %s", self.__pftablefile)
              try:
                 pf_file = open(self.__pftablefile, "a")
                 for host in new_hosts:
                    my_host = str(host)
                    pf_file.write("%s\n" % my_host)
                    info("Wrote new host %s to table file %s", my_host, self.__pftablefile)
                 pf_file.close()
              except Exception as e:
                  print(e)
                  print("Unable to write new host to PF table file.")
                  debug("Unable to write new host to PF table file %s", self.__pftablefile)


    #Check if host is blocked by the firewall (True means blocked).
    #Note: firewall_check() may be executed before firewall is set up for DenyHost, and should return False in such a situation.
    def firewall_check(self, host):
        my_host = str(host)
        check_result = False
        debug("Checking if host %s is blocked by firewall", my_host)
        if self.__iptables:
            try:
                if self.__ipset_name and self.__ipset_cmd:
                    args = [self.__ipset_cmd, "test", self.__ipset_name, my_host, "-quiet"]
                    debug("Checking if host is in the ipset: %s", " ".join(args))
                    check_result = system_execute(args, [0,1])  #returns 1 if ipset does not exist or host is not in ipset
                else:
                    if self.__blockport:
                        args = [self.__iptables, "-C", "INPUT", "-p", "tcp", "--dport", self.__blockport, "-s", my_host, "-j", "DROP"]
                    else:
                        args = [self.__iptables, "-C", "INPUT", "-s", my_host, "-j", "DROP"]
                    debug("Checking if firewall rule exists: %s", " ".join(args))
                    check_result = system_execute(args, [0,1])
            except Exception as e:
                print(e)
                print("Unable to check if firewall blocks host %s" % my_host)
                return False

        elif self.__pfctl and self.__pftable:
            raise RuntimeException("firewall_check not implemented for PF")

        if check_result == 0:
            debug("Host is already blocked by firewall")
            self.__fw_blocked_hosts.add(host)
            return True
        else:
            debug("Host is not blocked by firewall")
            return False


    def update_hosts_deny(self, deny_hosts):
        if not deny_hosts: return None, None

        #info("keys: %s", str( self.__denied_hosts.keys()))

        #Perform firewall host blocking
        #Temporarily we do not implement this logic for PF
        #if self.__iptables or (self.__pfctl and self.__pftable) or (self.__pftablefile):
        if self.__iptables:
            if not self.__fw_initialized:
                self.firewall_init()
            fw_hosts = [host for host in deny_hosts
                        if host not in self.__allowed_hosts
                        and host not in self.__fw_blocked_hosts
                        and not self.firewall_check(host)]
            if fw_hosts:
                self.firewall_block(fw_hosts)

        new_hosts = [host for host in deny_hosts
                     if host not in self.__denied_hosts
                     and host not in self.__allowed_hosts]

        if not new_hosts: return None, None
        debug("new hosts: %s", str(new_hosts))

        #When firewall_check is implemented for PF, remove this special logic
        if self.__pfctl and self.__pftable:
            self.firewall_block(new_hosts)

        try:
            fp = open(self.__prefs.get('HOSTS_DENY'), "a")
            status = 1
        except Exception as e:
            print(e)
            print("These hosts should be manually added to: %s", self.__prefs.get('HOSTS_DENY'))
            # print(self.__prefs.get('HOSTS_DENY'))
            fp = sys.stdout
            status = 0

        write_timestamp = self.__prefs.get('PURGE_DENY') is not None
        for host in new_hosts:
            block_service = self.__prefs.get('BLOCK_SERVICE')
            if block_service:
                block_service = "%s: " % block_service
                output = "%s%s%s" % (block_service, host, BSD_STYLE)
            else:
                output = "%s" % host

            if write_timestamp:
                fp.write("%s %s%s%s\n" % (DENY_DELIMITER,
                                          time.asctime(),
                                          ENTRY_DELIMITER,
                                          output))
            fp.write("%s\n" % output)

        if fp != sys.stdout:
            fp.close()

        plugin_deny = self.__prefs.get('PLUGIN_DENY')
        if plugin_deny: plugin.execute(plugin_deny, new_hosts)

        return new_hosts, status


    def is_valid(self, rx_match):
        invalid = 0
        try:
            if rx_match.group("invalid"):
                invalid = 1
        except Exception:
            invalid = 1
        return invalid

    def process_log(self, logfile, offset):
        try:
            if logfile.endswith(".gz"):
                fp = gzip.open(logfile)
            elif logfile.endswith(".bz2"):
                if HAS_BZ2: fp = bz2.BZ2File(logfile, "r")
                else:       raise Exception("Can not open bzip2 file (missing bz2 module)")
            else:
                fp = open(logfile, "r")
        except Exception as e:
            print("Could not open log file: %s" % logfile)
            print(e)
            return -1

        try:
            fp.seek(offset)
        except IOError:
            pass

        suspicious_always = is_true(self.__prefs.get('SUSPICIOUS_LOGIN_REPORT_ALLOWED_HOSTS'))

        login_attempt = LoginAttempt(self.__prefs,
                                     self.__allowed_hosts,
                                     suspicious_always,
                                     self.__first_time,
                                     1, # fetch all
                                     self.__restricted)

        for line in fp:
            success = invalid = 0
            m = None
            sshd_m = self.__sshd_format_regex.match(line)
            if sshd_m:
                message = sshd_m.group('message')

                # did this line match any of the fixed failed regexes?
                for i in FAILED_ENTRY_REGEX_RANGE:
                    rx = self.__failed_entry_regex_map.get(i)
                    if rx is None:
                        continue
                    m = rx.search(message)
                    if m:
                        invalid = self.is_valid(m)
                        break
                else: # didn't match any of the failed regex'es, was it succesful?
                    m = self.__successful_entry_regex.match(message)
                    if m:
                        success = 1

            # otherwise, did the line match a failed dovelog login attempt?
            if is_true(self.__prefs.get("DETECT_DOVECOT_LOGIN_ATTEMPTS")):
                rx =  self.__failed_dovecot_entry_regex
                m = rx.search(line)
                if m:
                    #debug("matched (host=%s): %s", m.group("host"), rx.pattern)
                    invalid = self.is_valid(m)

            # otherwise, did the line match one of the userdef regexes?
            if not m:
                for rx in self.__prefs.get('USERDEF_FAILED_ENTRY_REGEX'):
                    m = rx.search(line)
                    if m:
                        #info("matched: %s" % rx.pattern)
                        invalid = self.is_valid(m)
                        break

            if not m:
                # line isn't important
                continue

            try:
                user = m.group("user")
            except Exception:
                user = ""
            try:
                host = m.group("host")
            except Exception:
                error("regex pattern ( %s ) is missing 'host' group" % m.re.pattern)
                continue

            if not is_valid_ip_address(host):
                warning("got invalid host (%s), ignoring" % host)
                continue

            debug ("user: %s - host: %s - success: %d - invalid: %d",
                   user,
                   host,
                   success,
                   invalid)
            login_attempt.add(user, host, success, invalid)

        offset = fp.tell()
        fp.close()

        login_attempt.save_all_stats()
        deny_hosts = login_attempt.get_deny_hosts()

        #print deny_hosts
        new_denied_hosts, status = self.update_hosts_deny(deny_hosts)
        if new_denied_hosts:
            if not status:
                msg = "WARNING: Could not add the following hosts to %s" % self.__prefs.get('HOSTS_DENY')
            else:
                msg = "Added the following hosts to %s" % self.__prefs.get('HOSTS_DENY')
            self.__report.add_section(msg, new_denied_hosts)
            if self.__sync_server: self.sync_add_hosts(new_denied_hosts)
            plugin_deny = self.__prefs.get('PLUGIN_DENY')

            if plugin_deny: plugin.execute(plugin_deny, new_denied_hosts)

        new_suspicious_logins = login_attempt.get_new_suspicious_logins()
        if new_suspicious_logins:
            msg = "Observed the following suspicious login activity"
            self.__report.add_section(msg, list(new_suspicious_logins.keys()))

        if new_denied_hosts:
            info("new denied hosts: %s", str(new_denied_hosts))
        else:
            debug("no new denied hosts")

        if new_suspicious_logins:
            info("new suspicious logins: %s", str(list(new_suspicious_logins.keys())))
        else:
            debug("no new suspicious logins")

        if not self.__report.empty():
            if not self.__noemail:
                # send the report via email if configured
                send_email(self.__prefs, self.__report.get_report())
            elif not self.__daemon:
                # otherwise, if not in daemon mode, log the report to the console
                info(self.__report.get_report())
            self.__report.clear()

        return offset


    def sync_add_hosts(self, hosts):
        try:
            filename = os.path.join(self.__prefs.get("WORK_DIR"), SYNC_HOSTS)
            fp = open(filename, "a")
            for host in hosts:
                fp.write("%s\n" % host)
            fp.close()
            os.chmod(filename, 0o644)
        except Exception as e:
            error(str(e))

    def get_regex(self, name, default):
        val = self.__prefs.get(name)
        if not val:
            return default
        else:
            return re.compile(val)


    def init_regex(self):
        self.__sshd_format_regex = self.get_regex('SSHD_FORMAT_REGEX', SSHD_FORMAT_REGEX)

        self.__successful_entry_regex = self.get_regex('SUCCESSFUL_ENTRY_REGEX',
                                                       SUCCESSFUL_ENTRY_REGEX)

        self.__failed_entry_regex_map = {}
        for i in FAILED_ENTRY_REGEX_RANGE:
            if i == 1: extra = ""
            else: extra = "%i" % i
            self.__failed_entry_regex_map[i] = self.get_regex('FAILED_ENTRY_REGEX%s' % extra,
                                                              FAILED_ENTRY_REGEX_MAP[i])

        self.__failed_dovecot_entry_regex = self.get_regex('FAILED_DOVECOT_ENTRY_REGEX', FAILED_DOVECOT_ENTRY_REGEX)

##        self.__failed_entry_regex = self.get_regex('FAILED_ENTRY_REGEX', FAILED_ENTRY_REGEX)
##        self.__failed_entry_regex2 = self.get_regex('FAILED_ENTRY_REGEX2', FAILED_ENTRY_REGEX2)
##        self.__failed_entry_regex3 = self.get_regex('FAILED_ENTRY_REGEX3', FAILED_ENTRY_REGEX3)
##        self.__failed_entry_regex4 = self.get_regex('FAILED_ENTRY_REGEX4', FAILED_ENTRY_REGEX4)
##        self.__failed_entry_regex5 = self.get_regex('FAILED_ENTRY_REGEX5', FAILED_ENTRY_REGEX5)
##        self.__failed_entry_regex6 = self.get_regex('FAILED_ENTRY_REGEX6', FAILED_ENTRY_REGEX6)
##        self.__failed_entry_regex6 = self.get_regex('FAILED_ENTRY_REGEX7', FAILED_ENTRY_REGEX7)
##        self.__failed_entry_regex6 = self.get_regex('FAILED_ENTRY_REGEX8', FAILED_ENTRY_REGEX8)
##        self.__failed_entry_regex6 = self.get_regex('FAILED_ENTRY_REGEX9', FAILED_ENTRY_REGEX9)
##        self.__failed_entry_regex6 = self.get_regex('FAILED_ENTRY_REGEX10', FAILED_ENTRY_REGEX10)

# vim: set sw=4 et :
