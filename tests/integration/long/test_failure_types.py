# Copyright 2013-2015 DataStax, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


try:
    import unittest2 as unittest
except ImportError:
    import unittest


from cassandra.cluster import Cluster
from cassandra import ConsistencyLevel
from cassandra import WriteFailure, ReadFailure, FunctionFailure
from cassandra.concurrent import execute_concurrent_with_args
from cassandra.query import SimpleStatement
from tests.integration import use_singledc, PROTOCOL_VERSION, get_cluster, setup_keyspace


def setup_module():
    """
    We need some custom setup for this module. All unit tests in this module
    require protocol >=4. We won't bother going through the setup required unless that is the
    protocol version we are using.
    """

    # If we aren't at protocol v 4 or greater don't waste time setting anything up, all tests will be skipped
    if PROTOCOL_VERSION >= 4:
        use_singledc(start=False)
        ccm_cluster = get_cluster()
        ccm_cluster.stop()
        config_options = {'tombstone_failure_threshold': 2000, 'tombstone_warn_threshold': 1000}
        ccm_cluster.set_configuration_options(config_options)
        ccm_cluster.start(wait_for_binary_proto=True, wait_other_notice=True)
        setup_keyspace()


def teardown_module():
    """
    The rest of the tests don't need custom tombstones
    reset the config options so as to not mess with other tests.
    """
    if PROTOCOL_VERSION >= 4:
        ccm_cluster = get_cluster()
        config_options = {}
        ccm_cluster.set_configuration_options(config_options)
        if ccm_cluster is not None:
            ccm_cluster.stop()


class ClientExceptionTests(unittest.TestCase):

    def setUp(self):
        """
        Test is skipped if run with native protocol version <4
        """

        if PROTOCOL_VERSION < 4:
            raise unittest.SkipTest(
                "Native protocol 4,0+ is required for custom payloads, currently using %r"
                % (PROTOCOL_VERSION,))

        self.cluster = Cluster(protocol_version=PROTOCOL_VERSION)
        self.session = self.cluster.connect()
        self.nodes_currently_failing = []
        self.node1, self.node2, self.node3 = get_cluster().nodes.values()

    def tearDown(self):

        self.cluster.shutdown()
        failing_nodes = []

        # Restart the nodes to fully functional again
        self.setFailingNodes(failing_nodes, "testksfail")

    def setFailingNodes(self, failing_nodes, keyspace):
        """
        This method will take in a set of failing nodes, and toggle all of the nodes in the provided list to fail
        writes.
        @param failing_nodes A definitive list of nodes that should fail writes
        @param keyspace The keyspace to enable failures on

        """

        # Ensure all of the nodes on the list have failures enabled
        for node in failing_nodes:
            if node not in self.nodes_currently_failing:
                node.stop(wait_other_notice=True, gently=False)
                node.start(jvm_args=[" -Dcassandra.test.fail_writes_ks=" + keyspace], wait_for_binary_proto=True,
                           wait_other_notice=True)
                self.nodes_currently_failing.append(node)

        # Ensure all nodes not on the list, but that are currently set to failing are enabled
        for node in self.nodes_currently_failing:
            if node not in failing_nodes:
                node.stop(wait_other_notice=True, gently=False)
                node.start(wait_for_binary_proto=True, wait_other_notice=True)
                self.nodes_currently_failing.remove(node)

    def _perform_cql_statement(self, text, consistency_level, expected_exception):
        """
        Simple helper method to preform cql statements and check for expected exception
        @param text CQl statement to execute
        @param consistency_level Consistency level at which it is to be executed
        @param expected_exception Exception expected to be throw or none
        """
        statement = SimpleStatement(text)
        statement.consistency_level = consistency_level

        if expected_exception is None:
            self.session.execute(statement)
        else:
            with self.assertRaises(expected_exception):
                self.session.execute(statement)

    def test_write_failures_from_coordinator(self):
        """
        Test to validate that write failures from the coordinator are surfaced appropriately.

        test_write_failures_from_coordinator Enable write failures on the various nodes using a custom jvm flag,
        cassandra.test.fail_writes_ks. This will cause writes to fail on that specific node. Depending on the replication
        factor of the keyspace, and the consistency level, we will expect the coordinator to send WriteFailure, or not.


        @since 2.6.0
        @jira_ticket PYTHON-238
        @expected_result Appropriate write failures from the coordinator

        @test_category queries:basic
        """

        # Setup temporary keyspace.
        self._perform_cql_statement(
            """
            CREATE KEYSPACE testksfail
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '3'}
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=None)

        # create table
        self._perform_cql_statement(
            """
            CREATE TABLE testksfail.test (
                k int PRIMARY KEY,
                v int )
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=None)

        # Disable one node
        failing_nodes = [self.node1]
        self.setFailingNodes(failing_nodes, "testksfail")

        # With one node disabled we would expect a write failure with ConsistencyLevel of all
        self._perform_cql_statement(
            """
            INSERT INTO testksfail.test (k, v) VALUES  (1, 0 )
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=WriteFailure)

        # We have two nodes left so a write with consistency level of QUORUM should complete as expected
        self._perform_cql_statement(
            """
            INSERT INTO testksfail.test (k, v) VALUES  (1, 0 )
            """, consistency_level=ConsistencyLevel.QUORUM, expected_exception=None)

        failing_nodes = []

        # Restart the nodes to fully functional again
        self.setFailingNodes(failing_nodes, "testksfail")

        # Drop temporary keyspace
        self._perform_cql_statement(
            """
            DROP KEYSPACE testksfail
            """, consistency_level=ConsistencyLevel.ANY, expected_exception=None)

    def test_tombstone_overflow_read_failure(self):
        """
        Test to validate that a ReadFailure is returned from the node when a specified threshold of tombstombs is
        reached.

        test_tombstomb_overflow_read_failure First sets the tombstone failure threshold down to a level that allows it
        to be more easily encountered. We then create some wide rows and ensure they are deleted appropriately. This
        produces the correct amount of tombstombs. Upon making a simple query we expect to get a read failure back
        from the coordinator.


        @since 2.6.0
        @jira_ticket PYTHON-238
        @expected_result Appropriate write failures from the coordinator

        @test_category queries:basic
        """

        # Setup table for "wide row"
        self._perform_cql_statement(
            """
            CREATE TABLE test3rf.test2 (
                k int,
                v0 int,
                v1 int, PRIMARY KEY (k,v0))
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=None)

        statement = self.session.prepare("INSERT INTO test3rf.test2 (k, v0,v1) VALUES  (1,?,1)")
        parameters = [(x,) for x in range(3000)]
        execute_concurrent_with_args(self.session, statement, parameters, concurrency=50)

        statement = self.session.prepare("DELETE v1 FROM test3rf.test2 WHERE k = 1 AND v0 =?")
        parameters = [(x,) for x in range(2001)]
        execute_concurrent_with_args(self.session, statement, parameters, concurrency=50)

        self._perform_cql_statement(
            """
            SELECT * FROM test3rf.test2 WHERE k = 1
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=ReadFailure)

        self._perform_cql_statement(
            """
            DROP TABLE test3rf.test2;
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=None)

    def test_user_function_failure(self):
        """
        Test to validate that exceptions in user defined function are correctly surfaced by the driver to us.

        test_user_function_failure First creates a table to use for testing. Then creates a function that will throw an
        exception when invoked. It then invokes the function and expects a FunctionException. Finally it preforms
        cleanup operations.

        @since 2.6.0
        @jira_ticket PYTHON-238
        @expected_result Function failures when UDF throws exception

        @test_category queries:basic
        """

        # create UDF that throws an exception
        self._perform_cql_statement(
            """
            CREATE FUNCTION test3rf.test_failure(d double)
            RETURNS NULL ON NULL INPUT
            RETURNS double
            LANGUAGE java AS 'throw new RuntimeException("failure");';
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=None)

        # Create test table
        self._perform_cql_statement(
            """
            CREATE TABLE  test3rf.d (k int PRIMARY KEY , d double);
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=None)

        # Insert some values
        self._perform_cql_statement(
            """
            INSERT INTO test3rf.d (k,d) VALUES (0, 5.12);
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=None)

        # Run the function expect a function failure exception
        self._perform_cql_statement(
            """
            SELECT test_failure(d) FROM test3rf.d WHERE k = 0;
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=FunctionFailure)

        self._perform_cql_statement(
            """
            DROP FUNCTION test3rf.test_failure;
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=None)

        self._perform_cql_statement(
            """
            DROP TABLE test3rf.d;
            """, consistency_level=ConsistencyLevel.ALL, expected_exception=None)
