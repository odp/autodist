# Copyright 2021 Petuum, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import os
import ray
import tensorflow as tf
import tensorflow.compat.v1 as v1
from tensorflow.core.protobuf import config_pb2
from tensorflow.python.training.server_lib import ClusterSpec, Server

from autodist import AutoDist
from autodist.const import ENV, DEFAULT_GROUP_LEADER
from autodist.resource_spec import ResourceSpec
from autodist.resource_spec import DeviceSpec
from autodist.cluster import Cluster


@ray.remote
class TFServer:
    """ Tensorflow Server Actor responsible for executing the actual ops and
    storing parameters """

    def launch(self, cluster_spec, job_name, task_index, num_cpu_device):
        """ Start the TF server. This call blocks. """
        experimental = config_pb2.ConfigProto.Experimental(
            collective_nccl=True,
            collective_group_leader=DEFAULT_GROUP_LEADER)
        s = Server(
            ClusterSpec(cluster_spec),
            job_name=job_name,
            task_index=task_index,
            config=config_pb2.ConfigProto(
                experimental=experimental,
                device_count={"CPU": num_cpu_device},
                inter_op_parallelism_threads=0,
                intra_op_parallelism_threads=0,
            )
        )
        s.join()


class TFRunner:
    """ Each TFRunner including master represents one replica of the training job """

    def __init__(self,
                 strategy_builder,
                 strategy,
                 train_step,
                 model_fn,
                 input_fn,
                 env,
                 resource_spec):
        # Setup environment vars for the new runner
        for var, val in env.items():
            if type(val) == bool:
                os.environ[var] = "True" if val else "False"
            else:
                os.environ[var] = val

        # Set Ray backend to True
        os.environ[ENV.AUTODIST_RAY_BACKEND.name] = "True"

        # We either pass a strategy_builder or directly a strategy
        self._autodist = AutoDist(strategy_builder=strategy_builder,
                                  strategy=strategy,
                                  resource_spec=resource_spec)
        self._g = v1.Graph()
        with self._g.as_default(), self._autodist.scope():
            # model_fn and input_fn can return multiple things, pack and
            # unpack them into the step function
            models = model_fn()
            inputs = input_fn()
            if isinstance(inputs, tuple):
                iterators = (i.get_next() if hasattr(i, 'get_next')
                             else i for i in inputs)
            else:
                iterators = (inputs.get_next(),)

            if not isinstance(models, tuple):
                models = (models,)

            self._fetches = train_step(*models, *iterators)
            self._session = self._autodist.create_distributed_session()

    def step(self):
        """ Take one training step """
        with self._g.as_default(), self._autodist.scope():
            return self._session.run(self._fetches)

    def get_strategy(self):
        """ fetch the current strategy """
        return self._autodist._strategy


class TFTrainer:
    """ TFTrainer represents one training job. It starts master replica first,
    fetches the strategy from it and spawns the rest of the replicas if needed.
    """

    def __init__(self, strategy_builder, train_step, model_fn, input_fn):

        # Set Ray backend
        os.environ[ENV.AUTODIST_RAY_BACKEND.name] = "True"

        # Go from resource_info -> ResourceSpec -> ClusterSpec
        self._resource_spec = ResourceSpec(
            resource_info=self._get_resource_info())

        self._replicas = []   # Replica actors, also contains master

        # Start TF Servers on each node of the cluster
        self._servers = self._start_tf_servers(self._resource_spec)

        def spawn_replica(replica_host, strategy_builder, strategy=None, env={}):
            # Enforce actor placement on the provided host
            Runner = ray.remote(resources={f"node:{replica_host}": 0.01},
                                num_cpus=1)(TFRunner)
            return Runner.remote(strategy_builder,
                                 strategy,
                                 train_step,
                                 model_fn,
                                 input_fn,
                                 env,
                                 self._resource_spec)


        # Start the master worker, let it build a strategy from the strategy builder
        master = spawn_replica(ray._private.services.get_node_ip_address(), strategy_builder)

        # Add master to replicas list because it also acts as one of the clients
        self._replicas.append((ray._private.services.get_node_ip_address(), master))

        # Fetch the strategy directly from the master
        strategy = ray.get(master.get_strategy.remote())

        assert strategy is not None

        # Spawn clients based on the strategy built by master
        replica_devices = [
            DeviceSpec.from_string(device_string)
            for device_string in strategy.graph_config.replicas
        ]

        replica_hosts = {d.host_address for d in replica_devices}
        for replica_host in replica_hosts:
            if replica_host != ray._private.services.get_node_ip_address():
                # Only non-master replicas
                env = {
                    ENV.AUTODIST_WORKER.name: replica_host,
                    ENV.AUTODIST_MIN_LOG_LEVEL.name: ENV.AUTODIST_MIN_LOG_LEVEL.val,
                    ENV.AUTODIST_IS_TESTING.name: ENV.AUTODIST_IS_TESTING.val,
                    ENV.AUTODIST_PATCH_TF.name: ENV.AUTODIST_PATCH_TF.val,
                    ENV.AUTODIST_INTERNAL_TF.name: ENV.AUTODIST_INTERNAL_TF.val,
                    ENV.SYS_DATA_PATH.name: ENV.SYS_DATA_PATH.val,
                    ENV.SYS_RESOURCE_PATH.name: ENV.SYS_RESOURCE_PATH.val,
                }
                self._replicas.append((replica_host, spawn_replica(replica_host, None, strategy, env)))

    def _start_tf_servers(self, resource_spec):
        """ Launch TF server actors on each Ray nodes """

        cluster_spec = Cluster._get_default_cluster_spec(resource_spec)
        cpu_devices = Cluster._get_node_cpu_devices(resource_spec)
        gpu_devices = Cluster._get_node_gpu_devices(resource_spec)

        servers = []
        for job_name, tasks in cluster_spec.items():
            for task_index, full_address in enumerate(tasks):
                node_ip, _ = full_address.split(':')
                # Make sure we spawn one server per Ray node
                # Give it all the GPUs on that node
                server = TFServer.options(resources={f"node:{node_ip}": 0.01},
                                          num_gpus=len(gpu_devices.get(node_ip, []))).remote()
                servers.append(server)
                server.launch.remote(cluster_spec, 
                                     job_name, 
                                     task_index,
                                     len(cpu_devices[node_ip]))
        return servers

    def _get_resource_info(self):
        """ Create resource_info from resources available to the Ray cluster """

        resource_info = {}
        resource_info["nodes"] = []
        chief_address = ray._private.services.get_node_ip_address()
        for node in ray.nodes():
            node_ip = node["NodeManagerAddress"]
            cpu_count = node["Resources"].get("CPU")
            gpu_count = node["Resources"].get("GPU")
            if not node["Alive"] or (cpu_count is None and gpu_count is None):
                continue
            node = {"address": node_ip, 
                    "cpus": [0] if cpu_count else [], 
                    "gpus": list(range(int(gpu_count))) if gpu_count else []}
            if node_ip == chief_address:
                node["chief"] = True
            resource_info["nodes"].append(node)
        return resource_info

    def train(self):
        """ Runs a training epoch """
        
        return dict(zip([replica[0] for replica in self._replicas],
                        ray.get([replica[1].step.remote() for replica in self._replicas])))

    def shutdown(self):
        """ Shutdown all the actors and the training job """
        for server in self._servers:
            ray.kill(server)
        for replica in self._replicas:
            ray.kill(replica[1])

