# Copyright (c) 2023, 2024 Oracle and/or its affiliates.
#
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
#

import logging
from time import sleep
from utils import tutil
from utils import kutil
from utils import mutil
from utils.tutil import g_full_log
from utils.optesting import COMMON_OPERATOR_ERRORS

SQL_MY_ROLE = """
    SELECT MEMBER_ROLE
      FROM performance_schema.replication_group_members
     WHERE MEMBER_HOST = @@report_host
     """

class ClusterService(tutil.OperatorTest):
    default_allowed_op_errors = COMMON_OPERATOR_ERRORS
    instances = 2
    cluster_name = "mycluster"
    service_name = "mycluster" # should be as cluster_name
    secret_name = "mypwds"
    ann_name = "mycluster.example.com/ann"
    ann_value = "ann-value"
    label_name = "x-mylabel"
    label_value = "l-value"

    @classmethod
    def setUpClass(cls):
        cls.logger = logging.getLogger(__name__+":"+cls.__name__)
        super().setUpClass()

        for instance in range(0, cls.instances):
            g_full_log.watch_mysql_pod(cls.ns, f"{cls.cluster_name}-{instance}")

    @classmethod
    def tearDownClass(cls):
        for instance in reversed(range(0, cls.instances)):
            g_full_log.stop_watch(cls.ns, f"{cls.cluster_name}-{instance}")

        super().tearDownClass()

    def test_00_create(self):
        """
        Create cluster, check posted events.
        """
        kutil.create_user_secrets(
            self.ns, self.secret_name, root_user="root", root_host="%", root_pass="sakila")

        # create cluster with mostly default configs
        yaml = f"""
apiVersion: mysql.oracle.com/v2
kind: InnoDBCluster
metadata:
  name: {self.cluster_name}
spec:
  instances: {self.instances}
  router:
    instances: 1
  secretName: {self.secret_name}
  edition: community
  tlsUseSelfSigned: true
  service:
    defaultPort: mysql-rw-split
    labels:
      {self.label_name}: "{self.label_value}"
    annotations:
      {self.ann_name}: "{self.ann_value}"
"""

        kutil.apply(self.ns, yaml)

        self.wait_ic(self.cluster_name, ["PENDING", "INITIALIZING", "ONLINE"])

        for instance in range(0, self.instances):
            self.wait_pod(f"{self.cluster_name}-{instance}", "Running")

        self.wait_ic(self.cluster_name, "ONLINE", self.instances)

    def test_01_check_rw_split(self):
        with mutil.MySQLPodSession(self.ns, self.service_name, "root", "sakila",
                                   3306, "service") as s:
            # ensure we are in autocommit mode to be predicatble
            s.exec_sql("set autocommit=1")
            res = s.query_sql(SQL_MY_ROLE)
            self.assertEqual(res.fetch_one()[0], "SECONDARY")
            s.exec_sql("begin")
            res = s.query_sql(SQL_MY_ROLE)
            self.assertEqual(res.fetch_one()[0], "PRIMARY")

    def test_02_check_annotation_and_label(self):
        service = kutil.get_svc(self.ns, self.service_name)

        self.assertEqual(service['metadata']['annotations'][self.ann_name], self.ann_value)

        self.assertEqual(service['metadata']['labels'][self.label_name], self.label_value)

    def test_03_read_write(self):
        patch = {
            "spec": {
                "service": {
                    "defaultPort": "mysql-rw"
                }
            }
        }
        kutil.patch_ic(self.ns, self.cluster_name, patch, type="merge")
        sleep(1)
        with mutil.MySQLPodSession(self.ns, self.service_name, "root", "sakila",
                                   3306, "service") as s:
            res = s.query_sql(SQL_MY_ROLE)
            self.assertEqual(res.fetch_one()[0], "PRIMARY")


    def test_04_read_only(self):
        patch = {
            "spec": {
                "service": {
                    "defaultPort": "mysql-ro"
                }
            }
        }
        kutil.patch_ic(self.ns, self.cluster_name, patch, type="merge")
        sleep(1)
        with mutil.MySQLPodSession(self.ns, self.service_name, "root", "sakila",
                                   3306, "service") as s:
            res = s.query_sql(SQL_MY_ROLE)
            self.assertEqual(res.fetch_one()[0], "SECONDARY")

    def test_05_patch_annotation(self):
        new_ann_value = "new-value"
        patch = {
            "spec": {
                "service": {
                    "annotations": {
                        self.ann_name: new_ann_value
                    }
                }
            }
        }
        kutil.patch_ic(self.ns, self.cluster_name, patch, type="merge")
        sleep(1)
        service = kutil.get_svc(self.ns, self.service_name)

        self.assertEqual(service['metadata']['annotations'][self.ann_name], new_ann_value)

    def test_05_patch_label(self):
        new_label_name = "one-more"
        new_label_value = "new-label-value"
        patch = {
            "spec": {
                "service": {
                    "labels": {
                        new_label_name: new_label_value
                    }
                }
            }
        }
        kutil.patch_ic(self.ns, self.cluster_name, patch, type="merge")
        sleep(1)
        service = kutil.get_svc(self.ns, self.service_name)

        self.assertEqual(service['metadata']['labels'][new_label_name], new_label_value)

    def test_06_service_type_load_balancer(self):
        service = kutil.get_svc(self.ns, self.service_name)
        print(f'Old svc type is {service["spec"]["type"]}')
        svc_type = "LoadBalancer"
        patch = {
            "spec": {
                "service": {
                    "type": svc_type
                }
            }
        }
        kutil.patch_ic(self.ns, self.cluster_name, patch, type="merge")
        sleep(1)
        service = kutil.get_svc(self.ns, self.service_name)

        # we don't have any guarantee that we got an external IP and that we
        # can route there from anywhere, we can only verify we set the right
        # type
        self.assertEqual(service["spec"]["type"], svc_type)

    def test_99_destroy(self):
        kutil.delete_ic(self.ns, self.cluster_name)

        self.wait_pods_gone(f"{self.cluster_name}-*")
        self.wait_ic_gone(self.cluster_name)
