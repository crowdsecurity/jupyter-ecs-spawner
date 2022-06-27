import json
import os
import time
import base64
import pkgutil
import string
import copy

import boto3

import asyncio

import traitlets

from tornado import gen


from jupyterhub.spawner import Spawner

from concurrent.futures import ThreadPoolExecutor


class ECSSpawner(Spawner):

    custom_tags = traitlets.Dict(
        value_trait=traitlets.Unicode(), key_trait=traitlets.Unicode(), config=True
    )  # Dict of tags to apply to all instance
    ec2_ami = traitlets.Unicode(config=True)  # Id of the AMI to use for instance without GPU
    ec2_arm_ami = traitlets.Unicode(config=True)  # Id of the AMI to use for ARM instance without GPU
    ec2_gpu_ami = traitlets.Unicode(config=True)  # Id of the AMI to use for instance with GPU
    instance_role_arn = traitlets.Unicode(
        config=True
    )  # ARN of the role to associate with the EC2 instances. Must grant access to ECS.
    default_docker_image = traitlets.Unicode(
        config=True
    )  # Name of the docker image to use by default for the ECS task (eg, jupyter/datascience-notebook:notebook-6.4.0)
    default_docker_image_gpu = traitlets.Unicode(
        config=True
    )  # Name of the docker image to use by default for the ECS task, if the underlying instance has a GPU
    key_pair_name = traitlets.Unicode(
        config=True
    )  # Name of the keypair to associate with the instance. Optional, if not provided, you will not be able to SSH to the instance.
    use_public_ip = traitlets.Bool(default_value=False, config=True)

    USER_DATA_SCRIPT = """
    #!/bin/bash

    echo ECS_CLUSTER={0} >> /etc/ecs/ecs.config

    # Install AWS SSM for connecting to the instances
    arch=$(uname -i)
    if [[ $arch == x86_64* ]]; then
        sudo yum install -y https://s3.amazonaws.com/ec2-downloads-windows/SSMAgent/latest/linux_amd64/amazon-ssm-agent.rpm
    elif [[ $arch == i*86 ]]; then
        sudo yum install -y https://s3.amazonaws.com/ec2-downloads-windows/SSMAgent/latest/linux_386/amazon-ssm-agent.rpm
    elif [[ $arch == arm* ]]; then
        sudo yum install -y https://s3.amazonaws.com/ec2-downloads-windows/SSMAgent/latest/linux_arm64/amazon-ssm-agent.rpm
    else;
        echo "Architecture not found; SSM will not be installed."
    fi
    
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.state = []
        self.region = None
        self.instance_type = None
        self.instances = json.loads(pkgutil.get_data("ecsspawner", "instances.json"))
        self.amis = json.loads(pkgutil.get_data("ecsspawner", "amis.json"))
        self.check_config()

        self.instance_id = None
        self.task_definition_arn = None

        # AWS environment variables
        self.hub_host = os.environ["HUB_HOSTNAME"]
        self.task_role_arn = os.environ["TASK_ROLE_ARN"]
        self.efs_id = os.environ["EFS_ID"]
        self.efs_private_id = self.get_private_ids()
        self.subnet_id = os.environ["SUBNET_ID"]
        self.sg_id = [os.environ["SECURITY_GROUP_ID"]]
        self.ecs_cluster = os.environ["ECS_CLUSTER"]
        self.instance_role_arn = os.environ["INSTANCE_ROLE_ARN"]
        self.default_volume_size = os.environ["VOLUME_SIZE"]
        self.volume_size = int(os.environ["VOLUME_SIZE"])

        # Custom environment for notebook
        self.default_docker_image_gpu = os.environ["GPU_DOCKER_IMAGE"]

        self.custom_env = {
            "MLFLOW_TRACKING_URI": os.environ["MLFLOW_TRACKING_URI"],
            # "JUPYTERHUB_API_URL": f"http://{self.hub_host}:8081/hub/api",
            # "JUPYTERHUB_ACTIVITY_URL": f"http://{self.hub_host}:8081/hub/api/users/test/activity",
        }

    @staticmethod
    def get_private_ids():
        efs_client = boto3.client("efs")
        r = efs_client.describe_file_systems()

        all_ids = {}

        for fs in r["FileSystems"]:
            priv = False
            for tag in fs["Tags"]:
                if tag["Value"] == "private":
                    priv = True
                if tag["Key"] == "user":
                    name = tag["Value"]

            if priv:
                all_ids[name] = fs["FileSystemId"]

        return all_ids

    def check_config(self):
        # TODO
        pass

    async def start(self):
        self.instance_type = self.user_options["instance"]
        self.region = self.user_options["region"]
        if self.user_options["volume"] != "":
            self.volume_size = int(self.user_options["volume"])

        instance_id = await self.__spawn_ec2(self.user_options["instance"])
        with ThreadPoolExecutor(1) as executor:
            future = asyncio.wrap_future(executor.submit(self.__create_ecs_task))
            await asyncio.wrap_future(future)
            self.task_definition_arn = future.result()

        return (self.ip, 8888)

    async def poll(self):
        if self.task_definition_arn is None:
            return 0
        return None

    def terminate_instance(self):
        ec2_client = boto3.client("ec2", region_name=self.user_options["region"])
        ec2_client.terminate_instances(InstanceIds=[self.instance_id])
        waiter = ec2_client.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=[self.instance_id])

    async def stop(self, now=False):
        self.log.debug("Starting stop method")
        if self.instance_id:
            self.log.info("Terminating instance {0}".format(self.instance_id))
            with ThreadPoolExecutor(1) as executor:
                await asyncio.wrap_future(executor.submit(self.terminate_instance))
            self.log.info("Instance {0} terminated".format(self.instance_id))
            self.instance_id = None
            self.task_definition_arn = None
        else:
            self.log.debug("Stop called when no instance was created")

    async def progress(self):
        i = 0
        while True:
            events = self.state
            if i < (len(events) - 1):
                i += 1
                yield {"message": events[i]}
            await asyncio.sleep(1)

    def _options_form_default(self):
        tmpl = pkgutil.get_data("ecsspawner", "form_template.html").decode()
        s = string.Template(tmpl)
        return s.substitute(
            instance_json=pkgutil.get_data("ecsspawner", "instances.json").decode(),
            regions=pkgutil.get_data("ecsspawner", "regions.json").decode(),
        )

    def options_from_form(self, formdata):
        self.log.debug("Form args: {0}".format(formdata))
        return {
            "instance": formdata["instance"][0],
            "spot": formdata.get("spot"),
            "region": formdata["region"][0],
            "image": formdata["image"][0],
            "volume": formdata["volume"][0],
        }

    def __run_instance(self, ami, tpe, region):
        ec2_client = boto3.client("ec2", region_name=region)
        self.log.info("Requesting non spot instance of type {0}".format(tpe))
        run_args = {
            "ImageId": ami,
            "MinCount": 1,
            "MaxCount": 1,
            "InstanceType": tpe,
            "UserData": base64.b64encode(self.USER_DATA_SCRIPT.format(self.ecs_cluster).encode()).decode(),
            "InstanceInitiatedShutdownBehavior": "terminate",
            "IamInstanceProfile": {"Arn": self.instance_role_arn},
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {
                            "Key": "Name",
                            "Value": "jupyter-notebook-{0}".format(self.user.name),
                        }
                    ],
                }
            ],
        }
        if self.key_pair_name:
            run_args["KeyName"] = self.key_pair_name
        if self.subnet_id:
            self.log.info("Running in subnet {0}".format(self.subnet_id))
            run_args["SubnetId"] = self.subnet_id
        if self.sg_id:
            self.log.info("Adding security groups {0}".format(self.sg_id))
            run_args["SecurityGroupIds"] = self.sg_id

        run_args["BlockDeviceMappings"] = [
            {
                "DeviceName": self.__get_root_volume_name(ami),
                "Ebs": {"VolumeSize": self.volume_size, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ]
        instance = ec2_client.run_instances(**run_args)
        self.log.info("Starting EC2 instance")
        instance_id = instance["Instances"][0]["InstanceId"]
        waiter = ec2_client.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])
        if self.use_public_ip is True:
            self.ip = ec2_client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
                "PublicIpAddress"
            ]
        else:
            self.ip = ec2_client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
                "PrivateIpAddress"
            ]
        self.state.append("Instance running")
        self.log.info("EC2 instance is running (id: {0})".format(instance_id))
        return instance_id

    def __get_root_volume_name(self, ami):
        ec2_client = boto3.client("ec2", region_name=self.user_options["region"])
        r = ec2_client.describe_images(ImageIds=[ami])
        return r["Images"][0]["BlockDeviceMappings"][0]["DeviceName"]

    def __request_spot_instance(self, ami, tpe, region):
        ec2_client = boto3.client("ec2", region_name=region)
        self.log.info("Requesting spot instance")
        run_args = {
            "ImageId": ami,
            "InstanceType": tpe,
            "UserData": base64.b64encode(self.USER_DATA_SCRIPT.format(self.ecs_cluster).encode()).decode(),
            "IamInstanceProfile": {"Arn": self.instance_role_arn},
        }
        if self.key_pair_name:
            run_args["KeyName"] = self.key_pair_name
        if self.subnet_id:
            run_args["SubnetId"] = self.subnet_id
        if self.sg_id:
            run_args["SecurityGroupIds"] = self.sg_id

        run_args["BlockDeviceMappings"] = [
            {
                "DeviceName": self.__get_root_volume_name(ami),
                "Ebs": {"VolumeSize": self.volume_size, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ]
        spot_request = ec2_client.request_spot_instances(InstanceCount=1, LaunchSpecification=run_args)
        self.state.append("Spot request created")
        waiter = ec2_client.get_waiter("spot_instance_request_fulfilled")
        waiter.wait(SpotInstanceRequestIds=[spot_request["SpotInstanceRequests"][0]["SpotInstanceRequestId"]])
        self.state.append("Spot instance running")
        spot_instance = ec2_client.describe_spot_instance_requests(
            SpotInstanceRequestIds=[spot_request["SpotInstanceRequests"][0]["SpotInstanceRequestId"]]
        )
        instance_id = spot_instance["SpotInstanceRequests"][0]["InstanceId"]
        self.log.info("Spot instance is running (id: {0})".format(instance_id))
        ec2_client.create_tags(
            Resources=[instance_id],
            Tags=[{"Key": "Name", "Value": "jupyter-notebook-{0}".format(self.user.name)}],
        )
        if self.use_public_ip is True:
            self.ip = ec2_client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
                "PublicIpAddress"
            ]
        else:
            self.ip = ec2_client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
                "PrivateIpAddress"
            ]
        return instance_id

    async def __spawn_ec2(self, tpe):
        region = self.user_options["region"]
        if self.instances[region][tpe].get("gpu"):
            if self.ec2_gpu_ami != "":
                ami = self.ec2_gpu_ami
            else:
                ami = self.amis[region]["gpu"]
        elif self.instances[region][tpe]["arch"] == "x86_64" or self.instances[region][tpe]["arch"] == "i386":
            if self.ec2_ami != "":
                ami = self.ec2_ami
            else:
                ami = self.amis[region]["amd"]
        else:
            if self.ec2_arm_ami != "":
                ami = self.ec2_arm_ami
            else:
                ami = self.amis[region]["arm64"]

        self.log.info("Using AMI {0}".format(ami))
        if self.user_options.get("spot") is None:
            self.state.append("Requesting {0} non spot instance".format(tpe))
            spawn_method = self.__run_instance
        else:
            self.state.append("Requesting {0} spot instance".format(tpe))
            spawn_method = self.__request_spot_instance
        with ThreadPoolExecutor(1) as executor:
            future = asyncio.wrap_future(executor.submit(spawn_method, ami, tpe, region))
            await asyncio.wrap_future(future)
            self.instance_id = future.result()
        self.log.info("Finished spawning EC2")

    def __create_ecs_task(self):
        region = self.user_options["region"]
        ecs_client = boto3.client("ecs", region_name=region)
        max_tries = 200
        available_memory = 0
        available_cpu = 0
        self.state.append("Waiting for any instances to appear in ECS cluster")
        container_instances_arn = ecs_client.list_container_instances(cluster=self.ecs_cluster)["containerInstanceArns"]
        while not len(container_instances_arn):
            time.sleep(5)
            container_instances_arn = ecs_client.list_container_instances(cluster=self.ecs_cluster)[
                "containerInstanceArns"
            ]

        found = False
        attempt = 0
        self.state.append(
            f"Attempt {attempt} - waiting for {self.instance_id} to appear in ECS cluster and found={found}"
        )
        while (attempt < max_tries) and (not found):
            container_instances = ecs_client.describe_container_instances(
                cluster=self.ecs_cluster,
                containerInstances=ecs_client.list_container_instances(cluster=self.ecs_cluster)[
                    "containerInstanceArns"
                ],
            )["containerInstances"]
            for this_instance in container_instances:
                self.state.append(f"attempt {attempt} - {this_instance['ec2InstanceId']} - {self.instance_id}")
                if this_instance["ec2InstanceId"] == self.instance_id:
                    found = True
                    self.container_instance_arn = this_instance["containerInstanceArn"]
                    for res in this_instance["remainingResources"]:
                        if res["name"] == "CPU":
                            available_cpu = res["integerValue"]
                        if res["name"] == "MEMORY":
                            available_memory = res["integerValue"]

            time.sleep(6)
            attempt += 1
            if not attempt % 10:
                self.state.append(
                    f"Attempt {attempt} - waiting for {self.instance_id} to appear in ECS cluster and found={found}"
                )

        if not found:
            self.log.warn("Did not find container instance for {0}".format(self.instance_id))
            return None
        else:
            self.log.info("Found container instance for {0}".format(self.instance_id))
        ecs_client.put_attributes(
            cluster=self.ecs_cluster,
            attributes=[{"name": "jupyter-owner", "value": self.user.name, "targetId": self.container_instance_arn}],
        )

        if self.user_options["image"] != "":
            container_image = self.user_options["image"]
        else:
            if self.instances[region][self.user_options["instance"]].get("gpu") is not None:
                container_image = self.default_docker_image_gpu
            else:
                container_image = self.default_docker_image
        container_env = copy.deepcopy(self.get_env())
        # make this configurable ?
        container_env["GRANT_SUDO"] = "yes"
        container_env["NB_USER"] = self.user.name
        container_env["CHOWN_HOME"] = "yes"
        container_env["JUPYTER_ENABLE_LAB"] = "yes"
        for k, v in self.custom_env.items():
            container_env[k] = v

        self.log.info("Using docker image {0}".format(container_image))

        container_def = {
            "name": "jupyter-task-{0}".format(self.user.name),
            "image": container_image,
            "cpu": available_cpu,
            "memory": available_memory,
            "environment": [{"name": key, "value": value} for key, value in container_env.items()],
            "user": "root",
            "workingDirectory": "/home/{0}".format(self.user.name),
            "command": ["start-singleuser.sh"],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-region": region,
                    "awslogs-create-group": "true",
                    "awslogs-group": "/jupyterhub/jupyter-logs-{0}".format(self.user.name),
                },
            },
            "mountPoints": [
                {
                    "sourceVolume": "shared-persistent-volume",
                    "containerPath": f"/home/{self.user.name}/shared",
                    "readOnly": False,
                },
                {
                    "sourceVolume": "private-persistent-volume",
                    "containerPath": f"/home/{self.user.name}/private",
                    "readOnly": False,
                },
            ],
            "linuxParameters": {"sharedMemorySize": int(self.volume_size / 4) * 1000},
        }

        if self.instances[region][self.user_options["instance"]].get("gpu") is not None:
            num_gpus = self.instances[region][self.user_options["instance"]]["gpu"]["count"]
            print(f"gpus = {num_gpus}")
            container_def["resourceRequirements"] = [{"type": "GPU", "value": str(num_gpus)}]
            self.log.info(f"Using {num_gpus} x GPU")

        self.log.info("Creating ECS task def")
        self.state.append("Creating ECS task def")

        r = ecs_client.register_task_definition(
            family="jupyter-task-{0}".format(self.user.name),
            taskRoleArn=self.task_role_arn,
            networkMode="host",
            volumes=[
                {
                    "name": "shared-persistent-volume",
                    "efsVolumeConfiguration": {"fileSystemId": self.efs_id, "transitEncryption": "DISABLED"},
                },
                {
                    "name": "private-persistent-volume",
                    "efsVolumeConfiguration": {
                        "fileSystemId": self.efs_private_id[self.user.name],
                        "transitEncryption": "DISABLED",
                    },
                },
            ],
            containerDefinitions=[container_def],
        )
        self.log.info("ECS task created")
        self.task_definition_arn = r["taskDefinition"]["taskDefinitionArn"]
        self.log.info("Starting ECS task")
        self.state.append("Starting ECS task")
        r = ecs_client.start_task(
            cluster=self.ecs_cluster,
            containerInstances=[self.container_instance_arn],
            taskDefinition=self.task_definition_arn,
        )
        waiter = ecs_client.get_waiter("tasks_running")
        try:
            waiter.wait(cluster=self.ecs_cluster, tasks=[r["tasks"][0]["taskArn"]])
        except Exception as e:
            self.log.error("Exception while waiting for container to be up : {0}".format(e))
            return None
        self.log.info("ECS task is running")
        return self.task_definition_arn
