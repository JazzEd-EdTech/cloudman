import config
import logging
import logging.config
import os
import sys
from cm.clouds.cloud_config import CloudConfig
from cm.framework import messages
from cm.util import misc, paths

log = logging.getLogger('cloudman')
logging.getLogger('boto').setLevel(logging.INFO)


class CMLogHandler(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.formatter = logging.Formatter(
            "%(asctime)s - %(message)s", "%H:%M:%S")
        # self.formatter = logging.Formatter("[%(levelname)s]
        # %(module)s:%(lineno)d %(asctime)s: %(message)s")
        self.setFormatter(self.formatter)
        # Limit the size of the log message buffer to 1000 lines. This log is
        # used on the UI and causes responsivness issues once the log grows
        self.log_buffer = misc.RingBuffer(1000)

    @property
    def logmessages(self):
        return self.log_buffer.tolist()

    def emit(self, record):
        self.log_buffer.append(self.formatter.format(record))


class UniverseApplication(object):

    """
    Encapsulates the state of a Universe application
    """

    def __init__(self, **kwargs):
        print "Python version: ", sys.version_info[:2]
        self.PERSISTENT_DATA_VERSION = 3  # Current expected and generated PD version
        self.DEPLOYMENT_VERSION = 2
        # Instance persistent data file. This file gets created for
        # test/transient cluster types and stores the cluster config. In case
        # of a reboot, read the file to automatically recreate the services.
        self.INSTANCE_PD_FILE = '/mnt/persistent_data-current.yaml'
        cc = CloudConfig(app=self)
        # Get the type of cloud currently running on
        self.cloud_type = cc.get_cloud_type()
        # Create an appropriate cloud connection
        self.cloud_interface = cc.get_cloud_interface(self.cloud_type)
        # Read config file and check for errors
        self.config = config.Configuration(self, kwargs, self.cloud_interface.get_user_data())
        # From user data determine if object store (S3) should be used.
        self.use_object_store = self.config.get("use_object_store", True)
        # From user data determine if block storage (EBS/nova-volume) should be used.
        # (OpenNebula and dummy clouds do not support volumes yet so skip those)
        self.use_volumes = self.config.get(
            "use_volumes", self.cloud_type not in ['opennebula', 'dummy'])
#         self.config.init_with_user_data(self.ud)
        self.config.validate()
        # Setup logging
        self.logger = CMLogHandler()
        if "testflag" in self.config:
            self.TESTFLAG = bool(self.config['testflag'])
            self.logger.setLevel(logging.DEBUG)
        else:
            self.TESTFLAG = False
            self.logger.setLevel(logging.INFO)

        if "localflag" in self.config:
            self.LOCALFLAG = bool(self.config['localflag'])
            self.logger.setLevel(logging.DEBUG)
        else:
            self.LOCALFLAG = False
            self.logger.setLevel(logging.INFO)
        log.addHandler(self.logger)
        config.configure_logging(self.config)
        log.debug("Initializing app")
        log.debug("Running on '{0}' type of cloud in zone '{1}' using image '{2}'."
                  .format(self.cloud_type, self.cloud_interface.get_zone(),
                          self.cloud_interface.get_ami()))

        # App-wide object to store messages that need to travel between the back-end
        # and the UI.
        # TODO: Ideally, this should be stored some form of more persistent
        # medium (eg, database, file, session) and used as a simple module (vs. object)
        # but that's hopefully still forthcoming.
        self.msgs = messages.Messages()

        # App-wide consecutive number generator. Starting at 1, each time `next`
        # is called, get the next integer.
        self.number_generator = misc.get_a_number()

        # Check that we actually got user creds in user data and inform user
        if not ('access_key' in self.config or 'secret_key' in self.config):
            self.msgs.error("No access credentials provided in user data. "
                            "You will not be able to add any services.")
        # Update user data to include persistent data stored in cluster's bucket, if it exists
        # This enables cluster configuration to be recovered on cluster re-
        # instantiation
        self.manager = None
        pd = None
        if self.use_object_store and 'bucket_cluster' in self.config:
            log.debug("Looking for existing cluster persistent data (PD).")
            validate = True if self.cloud_type == 'ec2' else False
            if not self.TESTFLAG and misc.get_file_from_bucket(
                    self.cloud_interface.get_s3_connection(),
                    self.config['bucket_cluster'],
                    'persistent_data.yaml', 'pd.yaml',
                    validate=validate):
                        log.debug("Loading bucket PD file pd.yaml")
                        pd = misc.load_yaml_file('pd.yaml')
        # Have not found the file in the cluster bucket, look on the instance
        if not pd:
            if os.path.exists(self.INSTANCE_PD_FILE):
                log.debug("Loading instance PD file {0}".format(self.INSTANCE_PD_FILE))
                pd = misc.load_yaml_file(self.INSTANCE_PD_FILE)
        if pd:
            self.config.user_data = misc.merge_yaml_objects(self.config.user_data, pd)
            self.config.user_data = misc.normalize_user_data(self, self.config.user_data)
        else:
            log.debug("No PD to go by. Setting deployment_version to {0}."
                      .format(self.DEPLOYMENT_VERSION))
            # This is a new cluster so default to the current deployment version
            self.config.user_data['deployment_version'] = self.DEPLOYMENT_VERSION

    def startup(self):
        if 'role' in self.config:
            if self.config['role'] == 'master':
                log.debug("Master process starting.")
                from cm import master
                self.manager = master.ConsoleManager(self)
            elif self.config['role'] == 'worker':
                log.debug("Worker process starting.")
                from cm import worker
                self.manager = worker.ConsoleManager(self)
            self.path_resolver = paths.PathResolver(self.manager)
            self.manager.console_monitor.start()
        else:
            log.critical("************ No ROLE in %s - this is a fatal error. ************" % paths.USER_DATA_FILE)

    def shutdown(self, delete_cluster=False):
        if self.manager:
            self.manager.shutdown(delete_cluster=delete_cluster)
