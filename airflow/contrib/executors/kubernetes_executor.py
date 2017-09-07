# -*- coding: utf-8 -*-
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

import calendar
import logging
import time
import os
import multiprocessing
from queue import Queue
from datetime import datetime
from kubernetes import watch
from airflow import settings
from airflow.contrib.kubernetes.pod_launcher import PodLauncher
from airflow.executors.base_executor import BaseExecutor
from airflow.models import TaskInstance
from airflow.contrib.kubernetes.pod import Pod
from airflow.utils.state import State
from airflow import configuration
from kubernetes import client

class KubeConfig:
    @staticmethod
    def safe_get(section, option, default):
        try:
            return configuration.get(section, option)
        except AirflowConfigException:
            return default

    def __init__(self):
        self.dags_folder = configuration.get('core', 'dags_folder')
        self.parallelism = configuration.getint('core', 'PARALLELISM')
        self.kube_image = configuration.get('core', 'k8s_image')
        self.kube_namespace = self.safe_get('core', 'k8s_namespace', 'default')

        # These two props must be set together
        self.git_repo = self.safe_get('core', 'k8s_git_repo', None)
        self.git_branch = self.safe_get('core', 'k8s_git_branch', None)

        # Or these two props
        self.dags_volume_claim = self.safe_get('core', 'k8s_dags_volume_claim', None)
        self.dags_volume_subpath = self.safe_get('core', 'k8s_dags_volume_subpath', None)

        self._validate()

    def _validate(self):
        if self.dags_volume_claim and self.dags_volume_subpath:
            # do volume things
            pass
        elif self.git_repo and self.git_branch:
            # do git things
            pass
        else:
            raise AirflowConfigException(
                "Must set either: "
                "`k8s_dags_volume_claim and k8s_dags_volume_subpath` or "
                "`k8s_git_repo and k8s_git_branch`"
            )


class PodMaker:
    def __init__(self, kube_config):
        self.logger = logging.getLogger(__name__)
        self.kube_config = kube_config

    def _get_volumes_and_mounts(self):
        volume_name = "airflow-dags"

        if self.kube_config.dags_volume_claim:
            volumes = [{
                "name": volume_name, "persistentVolumeClaim": {"claimName": self.kube_config.dags_volume_claim}
            }]
            volume_mounts = [{
                "name": volume_name, "mountPath": self.kube_config.dags_folder,
                "readOnly": True, "subPath": self.kube_config.dags_volume_subpath
            }]
            return volumes, volume_mounts
        else:
            return [], []

    def _get_args(self, volumes, volume_mounts, airflow_command):
        if len(volumes) > 0 and len(volume_mounts) > 0:
            self.logger.info("Using k8s_dags_volume_claim for airflow dags")
            return [airflow_command]
        else:
            self.logger.info("Using git-syncher for airflow dags")
            cmd_args = "mkdir -p {dags_folder} && cd {dags_folder} &&" \
                       "git init && git remote add origin {git_repo} && git pull origin {git_branch} --depth=1 &&" \
                       "{command}".format(dags_folder=self.kube_config.dags_folder, git_repo=self.kube_config.git_repo,
                                          git_branch=self.kube_config.git_branch, command=airflow_command)
            return [cmd_args]

    def make_pod(self, namespace, pod_id, dag_id, task_id, execution_date, airflow_command):
        volumes, volume_mounts = self._get_volumes_and_mounts()

        pod = Pod(
            namespace=namespace,
            name=pod_id,
            image=self.kube_config.kube_image,
            cmds=["bash", "-cx", "--"],
            args=self._get_args(volumes, volume_mounts, airflow_command),
            labels={
                "airflow-slave": "",
                "dag_id": dag_id,
                "task_id": task_id,
                "execution_date": execution_date
            },
            envs={"AIRFLOW__CORE__EXECUTOR": "LocalExecutor"},
            volumes=volumes,
            volume_mounts=volume_mounts
        )
        return pod


class KubernetesJobWatcher(multiprocessing.Process, object):
    def __init__(self, watch_function, namespace, watcher_queue):
        self.logger = logging.getLogger(__name__)
        multiprocessing.Process.__init__(self)
        self._watch_function = watch_function
        self._watch = watch.Watch()
        self.namespace = namespace
        self.watcher_queue = watcher_queue

    def run(self):
        self.logger.info("Event: and now my watch begins")
        for event in self._watch.stream(self._watch_function, self.namespace,
                                        label_selector='airflow-slave'):
            task = event['object']
            self.logger.info("Event: {} had an event of type {}".format(task.metadata.name,
                                                                        event['type']))
            self.process_status(task.metadata.name, task.status.phase, task.metadata.labels)

    def process_status(self, job_id, status, labels):
        if status == 'Pending':
            self.logger.info("Event: {} Pending".format(job_id))
        elif status == 'Failed':
            self.logger.info("Event: {} Failed".format(job_id))
            self.watcher_queue.put((job_id, State.FAILED, labels))
        elif status == 'Succeeded':
            self.logger.info("Event: {} Succeeded".format(job_id))
            self.watcher_queue.put((job_id, None, labels))
        elif status == 'Running':
            # self.logger.info("Event: {} is Running".format(job_id))
            self.watcher_queue.put((job_id, State.RUNNING))
        else:
            self.logger.info("Event: Invalid state: {} on job: {} with labels: {}".format(status, job_id, labels))


class AirflowKubernetesScheduler(object):
    def __init__(self, task_queue, result_queue):
        self.logger = logging.getLogger(__name__)
        self.logger.info("creating kubernetes executor")
        self.kube_config = KubeConfig()
        self.task_queue = task_queue
        self.pending_jobs = set()
        self.namespace = os.environ['k8s_POD_NAMESPACE']
        self.logger.info("k8s: using namespace {}".format(self.namespace))
        self.launcher = PodLauncher()
        self.pod_maker = PodMaker(kube_config=self.kube_config)
        self.watcher_queue = multiprocessing.Queue()
        self.helper = KubernetesHelper()
        w = KubernetesJobWatcher(self.helper.pod_api.list_namespaced_pod, self.namespace, self.watcher_queue)
        w.start()

    def run_next(self, next_job):
        """

        The run_next command will check the task_queue for any un-run jobs.
        It will then create a unique job-id, launch that job in the cluster,
        and store relevent info in the current_jobs map so we can track the job's
        status

        :return: 

        """
        self.logger.info('k8s: job is {}'.format(str(next_job)))
        key, command = next_job
        dag_id, task_id, execution_date = key
        self.logger.info("running for command {}".format(command))
        pod_id = self._create_job_id_from_key(key=key)
        self.logger.info("k8s: launching image {}".format(self.kube_config.kube_image))
        pod = self.pod_maker.make_pod(
            namespace=self.namespace, pod_id=pod_id, dag_id=dag_id, task_id=task_id,
            execution_date=self._datetime_to_label_safe_datestring(execution_date), airflow_command=command
        )
        # the watcher will monitor pods, so we do not block.
        self.launcher.run_pod_async(pod)
        self.logger.info("k8s: Job created!")

    def delete_job(self, key):
        job_id = self._create_job_id_from_key(key)
        self.helper.delete_pod(job_id, namespace=self.namespace)

    def sync(self):
        """

        The sync function checks the status of all currently running kubernetes jobs.
        If a job is completed, it's status is placed in the result queue to 
        be sent back to the scheduler.

        :return:

        """
        while not self.watcher_queue.empty():
            self.process_watcher_task()

    def end_task(self):
        job_id, state, labels = self.watcher_queue.get()
        logging.info("Attempting to finish job; job_id: {}; state: {}; labels: {}".format(job_id, state, labels))
        key = self._labels_to_key(labels)
        if key:
            self.logger.info("finishing job {}".format(key))
            self.result_queue.put((key, state))

    @staticmethod
    def _create_job_id_from_key(key):
        keystr = '-'.join([str(x).replace(' ', '-') for x in key[:2]])
        job_fields = [keystr]
        unformatted_job_id = '-'.join(job_fields)
        job_id = unformatted_job_id.replace('_', '-')
        return job_id

    @staticmethod
    def _label_safe_datestring_to_datetime(string):
        """
        Kubernetes doesn't like ":" in labels, since ISO datetime format uses ":" but not "_" let's replace ":" with "_"
        :param string: string
        :return: datetime.datetime object
        """
        return datetime.strptime(string.replace("_", ":"), "%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def _datetime_to_label_safe_datestring(datetime_obj):
        """
        Kubernetes doesn't like ":" in labels, since ISO datetime format uses ":" but not "_" let's replace ":" with "_"
        :param datetime_obj: datetime.datetime object
        :return: ISO-like string representing the datetime
        """
        return datetime_obj.isoformat().replace(":", "_")

    def _labels_to_key(self, labels):
        try:
            return labels["dag_id"], labels["task_id"], self._label_safe_datestring_to_datetime(labels["execution_date"])
        except Exception as e:
            self.logger.warn("Error while converting labels to key; labels: {}; exception: {}".format(
                labels, e
            ))
            return None


class KubernetesExecutor(BaseExecutor):
    def __init__(self):
        super(KubernetesExecutor, self).__init__(parallelism=PARALLELISM)
        self.task_queue = None
        self._session = None
        self.result_queue = None
        self.pending_tasks = None
        self.kub_client = None

    def start(self):
        self.logger.info('k8s: starting kubernetes executor')
        self._session = settings.Session()
        self.task_queue = Queue()
        self.result_queue = Queue()
        self.kub_client = AirflowKubernetesScheduler(self.task_queue, self.result_queue)

    def sync(self):
        self.kub_client.sync()
        while not self.result_queue.empty():
            results = self.result_queue.get()
            self.logger.info("reporting {}".format(results))
            self.change_state(*results)

        if not self.task_queue.empty():
            (key, command) = self.task_queue.get()
            self.kub_client.run_next((key, command))

    def job_queue_full(self):
        return len(self.kub_client.current_jobs) > PARALLELISM

    def cluster_at_capacity(self):
        return len(self.pending_tasks) > 5

    def terminate(self):
        pass

    def change_state(self, key, state):
        self.logger.info("k8s: setting state of {} to {}".format(key, state))
        if state != State.RUNNING:
            self.kub_client.delete_job(key)
            try:
                self.running.pop(key)
            except KeyError:
                pass
        self.event_buffer[key] = state
        (dag_id, task_id, ex_time) = key
        item = self._session.query(TaskInstance).filter_by(
            dag_id=dag_id,
            task_id=task_id,
            execution_date=ex_time
        ).one()

        if item.state == State.RUNNING or item.state == State.QUEUED:
            item.state = state
            self._session.add(item)
            self._session.commit()

    def end(self):
        self.logger.info('ending kube executor')
        self.task_queue.join()

    def execute_async(self, key, command, queue=None):
        self.logger.info("k8s: adding task {} with command {}".format(key, command))
        self.task_queue.put((key, command))
