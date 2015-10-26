#!/usr/bin/env python
"""
Name: Ibp_server configuration script
Description: Helps in auto configuration of ibp_server.
"""
import socket
import fcntl
import struct
import sys
import os
import os.path as path
import commands
import re
import time
import array
import shutil
import argparse
import ConfigParser
import logging

# Setting basic logging
log = logging.getLogger('ibp_configure')
danger_pattern = re.compile(r"^/$|^/[boot|usr|var|lib|dev|bin|sbin|proc]+/?$")

MS_URL = "https://dlt.crest.iu.edu:9001"
IBP_CONFIG_LOG = "ibp_configure.log"

IBP_SAMPLE_CONFIG = """
# Do not modify this directly. It will loose change after service restart.
# Change in ibp_configure.py and then do 'service ibp-server restart'

[server]
interfaces={interfaces}
lazy_allocate=1
threads=16
log_file={ibp_log}
password=ibp
big_alloc_enable=1
substitute_map={substitute_map}

[phoebus]
{phoebus}

{resource}

[unis]
{unis}

"""

SYSCTL_CONFIG = """\n# Added by ibp_configure.py
net.ipv4.tcp_no_metrics_save = 1
net.ipv4.tcp_timestamps = 1
net.ipv4.tcp_sack = 1
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
net.ipv4.tcp_rmem = 4096 87380 33554432
net.ipv4.tcp_wmem = 4096 65536 33554432
net.core.netdev_max_backlog = 25000
"""

PHOEBUS_SAMPLE_CONFIG = "gateway={phoebus_gateway}"

UNIS_SAMPLE_CONFIG = """name = IBP Server
type = ibp_server
endpoint = {unis_endpoint}
protocol_name= ibp
registration_interval = 600
max_duration = {seconds}  
publicip = {public_ip}
publicport = {port}
use_ssl = {use_ssl}
client_certfile = {cert_file}
client_keyfile = {key_file}
institution = {inst}
country = {country}
state = {state}
zipcode = {zipcode}
email = {email}
latitude = {lat}
longitude = {lon}
"""

BLIPP_CONFIG = """{
    "status": "ON",
    "serviceType": "ps:tools:blipp",
    "name": "blipp",
    "ttl": 100000,
    "location": {
        "institution": "%s",
        "country": "%s",
        "state": "%s",
        "zipcode": "%s",
        "email": "%s",
        "latitude": %f,
        "longitude": %f
    },
    "description": "BLiPP for DLT Install",
    "properties": {
      "configurations": {
        "unis_url": "%s",
        "use_ssl": "%s",
            "ssl_cert": "%s",
            "ssl_key": "%s",
            "ssl_cafile": "%s",
        "probe_defaults":
        {"collection_schedule":"builtins.simple",
         "schedule_params": {"every": 5},
         "reporting_params": 8,
         "collection_size":10000,
         "collection_ttl":1500000,
         "ms_url": "%s"
        },
        "probes":{
               "net":{
                    "probe_module": "net"
                },
                "ibp_server": {
                    "probe_module": "cmd_line_probe",
                    "command": "get_version %s %s",
                    "regex": "Total resources.*Used:\\\s+(?P<used>\\\d+).*Free:\\\s+(?P<free>\\\d+).*",
                    "eventTypes": {"used": "ps:tools:blipp:ibp_server:resource:usage:used",
                                   "free": "ps:tools:blipp:ibp_server:resource:usage:free"}
                }
        }
      }
    }
}
"""

class System():
    def get_mount_point(self, pathname):
        "Get the mount point of the filesystem containing pathname"
        pathname= os.path.normcase(os.path.realpath(pathname))
        parent_device= path_device= os.stat(pathname).st_dev
        while parent_device == path_device:
            mount_point= pathname
            pathname= os.path.dirname(pathname)
            if pathname == mount_point: break
            parent_device= os.stat(pathname).st_dev
            return mount_point
        
    def get_mounted_device(self, pathname):
        "Get the device mounted at pathname"
        # uses "/proc/mounts"
        pathname= os.path.normcase(pathname) # might be unnecessary here
        try:
            with open("/proc/mounts", "r") as ifp:
                for line in ifp:
                    fields= line.rstrip('\n').split()
                    # note that line above assumes that
                    # no mount points contain whitespace
                    if fields[1] == pathname:
                        return fields[0]
        except EnvironmentError:
            pass
        return None # explicit

    def get_fs_freespace(self, pathname):
        "Get the free space of the filesystem containing pathname"
        stat= os.statvfs(pathname)
        # use f_bfree for superuser, or f_bavail if filesystem
        # has reserved space for superuser, in MB
        return stat.f_bfree*stat.f_bsize/(1024*1024)

    def execute_command(self, cmd, ignore_status = False):
        log.debug("Executing command: %s" % cmd)
        (status, output) = commands.getstatusoutput(cmd)
        if status and not ignore_status:
            log.error(output)
            sys.exit(1)
        return output

    def get_ip_address(self, ifname):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(fcntl.ioctl(
            s.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack('256s', ifname[:15])
        )[20:24])
    
    def all_interfaces(self):
        is_64bits = sys.maxsize > 2**32
        struct_size = 40 if is_64bits else 32
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        max_possible = 8 # initial value                                                                                                                                                                         
        while True:
            bytes = max_possible * struct_size
            names = array.array('B', '\0' * bytes)
            outbytes = struct.unpack('iL', fcntl.ioctl(
                s.fileno(),
                0x8912,  # SIOCGIFCONF                                                                                                                                                                           
                struct.pack('iL', bytes, names.buffer_info()[0])
            ))[0]
            if outbytes == bytes:
                max_possible *= 2
            else:
                break
        namestr = names.tostring()
        lst = []
        for i in range(0, outbytes, struct_size):
            name = namestr[i:i+16].split('\0', 1)[0]
            ip   = namestr[i+20:i+24]
            lst.append((name, ip))
        return lst

    def format_ip(self, addr):
        return str(ord(addr[0])) + '.' + \
            str(ord(addr[1])) + '.' + \
            str(ord(addr[2])) + '.' + \
            str(ord(addr[3]))


    def get_default_iface_name_linux(self):
        route = "/proc/net/route"
        with open(route) as f:
            for line in f.readlines():
                try:
                    iface, dest, _, flags, _, _, _, _, _, _, _, =  line.strip().split()
                    if dest != '00000000' or not int(flags, 16) & 2:
                        continue
                    return iface
                except:
                    continue

    def get_public_facing_ip_neuca(self, distro):
        from neuca_guest_tools import neuca
        customizer = neuca.NEucaOSCustomizer(distro)
        return customizer.getPublicIP()

    def get_public_facing_ip_using_default_interface(self):
        public_iface = self.get_default_iface_name_linux()
        return self.get_ip_address(public_iface)

    def get_public_facing_ip(self, args):
        if args.host:
            return args.host

        if args.neuca:
            try:
                return self.get_public_facing_ip_neuca(args.neuca)
            except:
                log.error('neuca get public ip failed, so trying to get ip of default interface')
                
        return self.get_public_facing_ip_using_default_interface()

class Configuration():
    def __init__(self):
        # init sets config defaults
        self.public_ip         = ""
        self.max_duration      = 2592000
        self.wait_interval     = 10
        self.enable_blipp      = False
        self.blipp_sysoptfile  = "/etc/sysconfig/blippd"
        self.phoebus           = ""
        self.unis_endpoint     = "https://dlt.crest.iu.edu:9000"
        self.unis_use_ssl      = True
        self.unis_cert_file    = "/usr/local/etc/dlt-client.pem"
        self.unis_key_file     = "/usr/local/etc/dlt-client.key"
        self.unis_ca_file      = "/usr/local/etc/dlt-ca.bundle"
        self.unis_institution  = ""
        self.unis_state        = ""
        self.unis_zipcode      = ""
        self.unis_email        = ""
        self.unis_country      = ""
        self.unis_latitude     = 0
        self.unis_longitude    = 0
        self.ibp_do_res        = False
        self.ibp_size          = 8000
        self.ibp_host          = ""
        self.ibp_port          = 6714
        self.ibp_resource_path = "/tmp/ibp_resources"
        self.ibp_resource_db   = "/tmp/ibp_resources/db"
        self.ibp_root          = "/"
        self.ibp_log           = "/var/log/ibp_server.log"
        self.ibp_sub_ip        = ""
        self.ibp_sysctl        = "/etc/sysctl.d/ibp.conf"
        
    def allocation_success_file(self):
        # acts as lock for reallocation. This file will be created when resources are
        # allocated for ibp_server. If this file is removed then resources will be
        # reallocated (and all original data will be lost)
        return os.path.join(self.ibp_resource_path, ".allocations_do_not_remove")

    def makefs_cmd(self):
        return path.join(self.ibp_root, "bin/mkfs.resource") + " 1 dir " + self.ibp_resource_path\
               + " " + self.ibp_resource_db + " -b " + str(self.ibp_size)\
               + " -d " + str(self.max_duration)

    def ibp_config_file(self):
        return path.join(self.ibp_root, "etc/ibp.cfg")

    def blipp_config_file(self):
        # check that etc/periscope exists
        filename = "/etc/periscope/blipp_dlt.json"
        if not os.path.exists(os.path.dirname(filename)):
            os.makedirs(os.path.dirname(filename))
        return path.join(self.ibp_root, filename)

    def ibp_interface_monitor(self):
        return path.join(self.ibp_root, "bin/ibp_interface_monitor.py") + " -l -d"

    def query_yes_no(self, question, default="no"):
        """Ask a yes/no question via raw_input() and return their answer.
        "question" is a string that is presented to the user.
        "default" is the presumed answer if the user just hits <Enter>.
        It must be "yes" (the default), "no" or None (meaning
        an answer is required of the user).
        
        The "answer" return value is one of "yes" or "no".
        """
        valid = {"yes":True,   "y":True,  "ye":True,
                 "no":False,     "n":False}
        if default == None:
            prompt = " [y/n] "
        elif default == "yes":
            prompt = " [Y/n] "
        elif default == "no":
            prompt = " [y/N] "
        else:
            raise ValueError("invalid default answer: '%s'" % default)
            
        while True:
            sys.stdout.write(question + prompt)
            choice = raw_input().lower()
            if default is not None and choice == '':
                return valid[default]
            elif choice in valid:
                return valid[choice]
            else:
                sys.stdout.write("Please respond with 'yes' or 'no' "\
                                 "(or 'y' or 'n').\n")

    def get_ibp_interface_addresses(self, args):
        """
        """
        interface_addresses = ""
        if args.interfaces:
            interfaces = args.interfaces.split(",")
            for ip in interfaces:
                interface_addresses += ip + ":" + str(self.ibp_port) + ";"
        else:
            for name, ip in mysys.all_interfaces():
                if name != "lo" or ip.startswith("127."):
                    interface_addresses += mysys.format_ip(ip) + ":" + str(self.ibp_port) + ";"

        if args.extra_nat_iface:
            interface_addresses += args.extra_nat_iface+";"            
                    
        if interface_addresses == "":
            log.error("not even single interface address could be determined")
            sys.exit(1)

        return interface_addresses

    def reallocation_needed(self, args):
        """
        We allocate resources when either lock file is not present or force_allocate flag is present
        """
        if args.force_allocate:
            return True
            
        if not args.geni and os.path.exists('/tmp/ibp_dbenv'):
            do_del = self.query_yes_no(' Found existing IBP DB environment, delete', default="yes")
            if do_del:
                shutil.rmtree('/tmp/ibp_dbenv')
            else:
                log.info("WARNING: Existing DB environment in /tmp/ibp_dbenv might conflict\n\
                with new resource configurations!")

        if self.ibp_do_res and os.path.isfile(self.allocation_success_file()):
            log.info('This text file ({0}) acts as a lock for resource allocation.\n\
       Delete it to allow re-allocation!'.format(self.allocation_success_file()))
            return False
        elif self.ibp_do_res:
            log.info('This text file ({0}) not found, so allocating the '
                     'resources'.format(self.allocation_success_file()))
            return True

        return False

    def allocate(self, args):
        """
        Deletes resource directories and allocates them back
        """
        if not self.reallocation_needed(args):
            if self.ibp_do_res:
                log.info("Including previous resource configuration")
                with open(self.allocation_success_file(), 'r') as f:
                    return f.read()
            else:
                return ""

        if path.exists(self.ibp_resource_path) and path.exists(self.ibp_resource_db):
            resource = mysys.execute_command(self.makefs_cmd())
        else:
            log.error("Resource and/or DB path do not exist: [%s, %s]" %
                      self.ibp_resource_path, self.ibp_resource_db)
            exit(1)

        # save resource for later runs
        with open(self.allocation_success_file(), 'w') as f:
            f.write(resource)

        return resource

    def path_check_create(self, path, disp, dval):
        path = self.get_string(disp, dval)

        # check if already allocated
        if os.path.exists(path):
            if danger_pattern.match(path):
                log.info("Specified path is an OS system directory, please try again")
                return path, False
            ret = self.query_yes_no("WARNING: directory %s already exists, delete?" % path)
            if ret:
                shutil.rmtree(path)
            else:
                return path, False
        os.makedirs(path)
        return path, True

    def save_and_append(self, fname, content):
        # check that path exists
        if not os.path.isdir(os.path.dirname(fname)):
            os.makedirs(os.path.dirname(fname))
            
        with open(fname, 'a') as f:
            f.write(content)

    def save_and_write(self, fname, content):
        # check that path exists
        if not os.path.isdir(os.path.dirname(fname)):
            os.makedirs(os.path.dirname(fname))

        if os.path.isfile(fname):
            with open(fname, 'r') as f:
                with open(fname + ".ibp_configure_save", 'w') as g:
                    g.write(f.read())
                    g.close()
                    f.close()

        with open(fname, 'w') as f:
            f.write(content)

    def get_string(self, disp_str, dval):
        val = raw_input(disp_str)
        if len(str(val)):
            return str(val)
        else:
            return dval

    def get_int(self, disp_str, dval):
        val = raw_input(disp_str)
        try:
            rval = int(val)
            return rval
        except:
            return dval

    def get_real(self, disp_str, dval):
        val = raw_input(disp_str)
        try:
            rval = float(val)
            return rval
        except:
            return dval

    def get_user_input(self, args):
        self.public_ip = mysys.get_public_facing_ip(args)

        log.info("===============================================================")
        log.info(":: Begin interactive DLT configuration")
        log.info('')
        log.info("== IBP Server Settings ==")
        self.ibp_host = self.get_string(' IBP hostname [%s]: ' % self.public_ip, self.public_ip) 
        self.ibp_port = self.get_int(' IBP port [%s]: ' % self.ibp_port, self.ibp_port)
        self.ibp_log = self.get_string(' IBP log file [%s] ' % self.ibp_log, self.ibp_log)
        self.ibp_do_res = self.query_yes_no(' Configure an initial IBP resource', default="yes")
        if self.ibp_do_res:
            is_valid = False
            while not is_valid:
                self.ibp_resource_path, is_valid = self.path_check_create(self.ibp_resource_path,
                                                                          ' Resource path [%s] ' % self.ibp_resource_path,
                                                                          self.ibp_resource_path)

            is_valid = False
            while not is_valid:
                self.ibp_resource_db, is_valid = self.path_check_create(self.ibp_resource_db,
                                                                        ' Resource DB path [%s] ' % self.ibp_resource_db,
                                                                        self.ibp_resource_db)
            size = mysys.get_fs_freespace(self.ibp_resource_path)
            self.ibp_size = self.get_int(' Usable disk space [%s MB] ' % size, size)  
            duration = self.max_duration
            self.max_duration = self.get_int(' Max duration for allocation [%s seconds] ' % duration, duration)

        log.info('')
        log.info("== UNIS Settings (depot registration) ==")
        self.unis_endpoint = self.get_string(' UNIS URL [%s]: ' % self.unis_endpoint, self.unis_endpoint)
        self.unis_use_ssl = self.query_yes_no(' Enable SSL', default="yes")
        if self.unis_use_ssl:
            self.unis_cert_file = self.get_string(' UNIS client cert file [%s]: ' %
                                                  self.unis_cert_file, self.unis_cert_file)
            self.unis_key_file = self.get_string(' UNIS client key file [%s]: ' %
                                                 self.unis_key_file, self.unis_key_file)
        self.unis_institution = self.get_string(' Institution [%s]: ' % "", "unknown")
        self.unis_country = self.get_string(' Country [%s]: ' % "US", "US")
        self.unis_state = self.get_string(' State [%s]: ' % "AK", "AK")
        self.unis_zipcode = self.get_string(' ZipCode [%s]: ' % "", "00000")
        self.unis_email = self.get_string(' Admin email [%s]: ' % "", "dlt@crest.iu.edu")
        self.unis_latitude = self.get_real(' Latitude [%s]: ' % self.unis_latitude, self.unis_latitude) 
        self.unis_longitude = self.get_real(' Longitude [%s]: ' % self.unis_longitude, self.unis_longitude) 
        self.enable_blipp = self.query_yes_no(' Monitor the depot with BLiPP (usage stats)', default='yes')
        log.info('')
        log.info("== Phoebus Settings (WAN Acceleration) ==")
        self.phoebus = self.get_string(' Optional Phoebus Gateway (<host>/<port>): ', '')
        log.info('')
        log.info("== System Settings ==")
        self.systune = self.query_yes_no(' Apply network tuning to improve TCP performance (sysctl)', default='yes')
        log.info('')
        log.info("End DLT configuration")
        log.info("===============================================================")

    def generate_blipp_config(self, args):
        blipp_config = BLIPP_CONFIG % (self.unis_institution,
                                       self.unis_state,
                                       self.unis_zipcode,
                                       self.unis_email,
                                       self.unis_country,
                                       self.unis_latitude,
                                       self.unis_longitude,
                                       self.unis_endpoint,
                                       self.unis_use_ssl,
                                       self.unis_cert_file,
                                       self.unis_key_file,
                                       self.unis_ca_file,
                                       MS_URL,
                                       self.ibp_host,
                                       self.ibp_port)
        
        self.save_and_write(self.blipp_config_file(), blipp_config)
        # update default blipp options
        blipp_opts = 'OPTIONS="-c %s"\n' % self.blipp_config_file()
        self.save_and_write(self.blipp_sysoptfile, blipp_opts)
        return blipp_config

    def generate_ibp_config(self, args):
        """
        """
        resource_config = self.allocate(args)

        if self.phoebus == "":
            phoebus_config = ""
        else:
            phoebus_config = PHOEBUS_SAMPLE_CONFIG.format(phoebus_gateway=self.phoebus)

        if len(self.ibp_host):
            ibp_conn_strings = self.ibp_host+':'+str(self.ibp_port)+";"
        else:
            ibp_conn_strings = self.get_ibp_interface_addresses(args)

        if args.extra_nat_iface:
	    ibp_conn_strings += args.extra_nat_iface+";" 

        unis_config = UNIS_SAMPLE_CONFIG.format(unis_endpoint=self.unis_endpoint,
                                                public_ip=self.ibp_host,
                                                port=self.ibp_port,
                                                seconds=self.max_duration,
                                                use_ssl=int(self.unis_use_ssl),
                                                cert_file=self.unis_cert_file,
                                                key_file=self.unis_key_file,
                                                ca_file=self.unis_ca_file,
                                                inst=self.unis_institution,
                                                state=self.unis_state,
                                                zipcode=self.unis_zipcode,
                                                email=self.unis_email,
                                                country=self.unis_country,
                                                lat=self.unis_latitude,
                                                lon=self.unis_longitude)

        ibp_config = IBP_SAMPLE_CONFIG.format(interfaces=ibp_conn_strings,
                                              ibp_log=self.ibp_log,
                                              substitute_map=self.ibp_sub_ip,
                                              phoebus=phoebus_config,
                                              resource=resource_config,
                                              unis=unis_config)

        self.save_and_write(self.ibp_config_file(), ibp_config)
        return ibp_config


def configure_logging(log_to_file, debug=False):
    global log
    if debug:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)
    formatter = logging.Formatter('[%(levelname)s] %(message)s')
    if log_to_file:
        handler = logging.FileHandler(LOGFILENAME, 'a')
        handler.setFormatter(formatter)
        log.addHandler(handler)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        log.addHandler(handler)


mysys = System()

def main():
    parser = argparse.ArgumentParser(
        description="Allocates resources and creates ibp.cfg file")
    parser.add_argument('--neuca', type=str, default=None,
                        help='Use neuca tools to get public ip. Provide distro name as parameter'
                        'Supported distributions are debian, ubuntu, redhat, centos, fedora.')
    parser.add_argument('--debug', action='store_true',
                        help='Turn on debugging output.')
    parser.add_argument('--force-allocate', action='store_true',
                        help='Ignores the resource lock and reallocates the resources.')
    parser.add_argument('--host', type=str, default=None,
                        help='Specify hostname or IP of the node. If specified this script will not attempt\
                        to guess the public IP.')
    parser.add_argument('-i', '--interfaces', type=str, default=None,
                        help='List of interfaces to bind to. If this option is not used then '
                              'all interfaces except localhost will be set. Specify multiple '
                              'interfaces by a comma separated list')
    parser.add_argument('--ibp-resource-dir', type=str, default='/root',
			help='A path for the ibp_resource and db directories.')
    parser.add_argument('--ibp-root', type=str, default='/',
                        help='Change the relative install path, default is /')
    parser.add_argument('--geni', action='store_true',
                        help='Non-interactive config generation for GENI deployments.')
    parser.add_argument('--extra-sub-map', type=str, default=None,
                        help='Add any static <host>:<host> substitutions for NAT configurations.')
    parser.add_argument('--extra-nat-iface', type=str, default=None,
                        help='Add any static <host>:<port> interfaces for NAT configurations.')
    parser.add_argument('-l', '--log', action='store_true', help='Log to file.')
    args = parser.parse_args()

    #only either of neuca or public ip should be set
    if args.neuca and args.host:
        log.error("--neuca and --host should not be used together")
        sys.exit(1)

    configure_logging(args.log, debug=args.debug)

    cfg = Configuration()
    cfg.ibp_root = args.ibp_root

    if (args.geni):
        cfg.unis_endpoint     = "http://monitor.crest.iu.edu:9000"
        cfg.unis_use_ssl      = False
        cfg.ibp_do_res        = True
        cfg.public_ip         = mysys.get_public_facing_ip(args)
        cfg.max_duration      = args.max_duration
        cfg.ibp_resource_path = args.ibp_resource_dir + "/ibp_resources"
        cfg.ibp_resource_db   = args.ibp_resource_dir + "/ibp_resources/db"
        cfg.ibp_size          = mysys.get_fs_freespace(args.ibp_resource_dir)
        if args.neuca:
            default_ip        = mysys.get_public_facing_ip_using_default_interface()
            cfg.ibp_sub_ip    = default_ip+":"+cfg.public_ip+";"
        if args.extra_sub_map:
            cfg.ibp_sub_ip   += args.extra_sub_map+";"

    else:
        cfg.get_user_input(args)
    
    log.info("Saving IBP configuration to %s" % cfg.ibp_config_file())
    ibp_config = cfg.generate_ibp_config(args)
    log.debug(ibp_config)

    if (cfg.enable_blipp):
        log.info("Saving BLiPP configuration to %s" % cfg.blipp_config_file())
        log.info("  Start BLiPP using systemctl or service")
        blipp_config = cfg.generate_blipp_config(args)
        log.debug(blipp_config)

    if (cfg.systune):
        cfg.save_and_write(cfg.ibp_sysctl, SYSCTL_CONFIG)
        mysys.execute_command("sysctl --system")
        log.info("Added sysctl settings and ran 'sysctl --system' to apply network tuning")
    
    # start interface monitoring thread
    # execute_command(c.ibp_interface_monitor(), True)

    # bbye
    sys.exit(0)

if __name__ == "__main__":
    main()
