
import json
import time
import base64
import pkgutil
import string


import boto3

import asyncio

import traitlets

from tornado import gen


from jupyterhub.spawner import Spawner

from concurrent.futures import ThreadPoolExecutor

class ECSSpawner(Spawner):

    security_groups_ids = traitlets.List(traitlets.Unicode(), config=True) # List of security group for the EC2 instance
    custom_tags = traitlets.Dict(value_trait=traitlets.Unicode(), key_trait=traitlets.Unicode(), config=True) #Dict of tags to apply to all instance
    ec2_ami = traitlets.Unicode(config=True)  # Id of the AMI to use for instance without GPU
    ec2_arm_ami = traitlets.Unicode(config=True)  # Id of the AMI to use for ARM instance without GPU
    ec2_gpu_ami = traitlets.Unicode(config=True) # Id of the AMI to use for instance with GPU
    subnet_id = traitlets.Unicode(config=True)
    ecs_cluster = traitlets.Unicode(default_value='default', config=True) # The name of the ECS cluster we want to use
    instance_role_arn = traitlets.Unicode(config=True) # ARN of the role to associate with the EC2 instances. Must grant access to ECS.
    default_docker_image = traitlets.Unicode(config=True) # Name of the docker image to use by default for the ECS task (eg, jupyter/datascience-notebook:notebook-6.4.0)
    default_docker_image_gpu = traitlets.Unicode(config=True) # Name of the docker image to use by default for the ECS task, if the underlying instance has a GPU
    key_pair_name = traitlets.Unicode(config=True) #Name of the keypair to associate with the instance. Optional, if not provided, you will not be able to SSH to the instance.
    use_public_ip = traitlets.Bool(default_value=False, config=True)
    custom_env = traitlets.Dict(value_trait=traitlets.Unicode(), key_trait=traitlets.Unicode(), config=True) #Dict of environment variables to be added to the container


    USER_DATA_SCRIPT = """
    #!/bin/bash

    echo ECS_CLUSTER={0} >> /etc/ecs/ecs.config
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.instances = json.loads(pkgutil.get_data("ecsspawner", "instances.json"))
        self.amis = json.loads(pkgutil.get_data("ecsspawner", "amis.json"))
        self.check_config()

    def check_config(self):
        #TODO
        pass

    async def start(self):
        self.instance_id = None
        self.task_definition_arn = None
        self.instance_type = self.user_options['instance']
        self.region = self.user_options['region']
        self.state = []

        instance_id = await self.__spawn_ec2(self.user_options['instance'])
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
        ec2_client = boto3.client('ec2', region_name=self.user_options['region'])
        ec2_client.terminate_instances(
                    InstanceIds=[
                        self.instance_id,
                    ]
        )
        waiter = ec2_client.get_waiter('instance_terminated')
        waiter.wait(
            InstanceIds=[
                self.instance_id,
            ]
        )


    async def stop(self):
        self.log.debug('Starting stop method')
        if self.instance_id:
            self.log.info('Terminating instance {0}'.format(self.instance_id))
            with ThreadPoolExecutor(1) as executor:
                await asyncio.wrap_future(executor.submit(self.terminate_instance))
            self.log.info('Instance {0} terminated'.format(self.instance_id))
            self.instance_id = None
            self.task_definition_arn = None
        else:
            self.log.debug('Stop called when no instance was created')

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
        return s.substitute(instance_json=pkgutil.get_data("ecsspawner", "instances.json").decode(), regions=pkgutil.get_data("ecsspawner", "regions.json").decode())

    def options_from_form(self, formdata):
        self.log.debug("Form args: {0}".format(formdata))
        return {'instance': formdata['instance'][0], 'spot': formdata.get('spot'), 'region': formdata['region'][0], 'image': formdata['image'][0], 'volume': formdata['volume'][0]}


    def __run_instance(self, ami, type, region):
        ec2_client = boto3.client('ec2', region_name=region)
        self.log.info('Requesting non spot instance of type {0}'.format(type))
        run_args = {
                'ImageId': ami,
                'MinCount': 1,
                'MaxCount': 1,
                'InstanceType': type,
                'UserData': base64.b64encode(self.USER_DATA_SCRIPT.format(self.ecs_cluster).encode()).decode(),
                'InstanceInitiatedShutdownBehavior': 'terminate',
                'IamInstanceProfile': {"Arn":self.instance_role_arn},
                'TagSpecifications': [ 
                        {
                            "ResourceType": "instance", 
                            "Tags": [{"Key": "Name", "Value": "jupyter-notebook-{0}".format(self.user.name) }]
                        }
                ]
        }
        if self.key_pair_name:
            run_args['KeyName'] = self.key_pair_name
        if self.subnet_id:
            self.log.info('Running in subnet {0}'.format(self.subnet_id))
            run_args['SubnetId'] = self.subnet_id
        if self.security_groups_ids:
            self.log.info('Adding security groups {0}'.format(self.security_groups_ids))
            run_args['SecurityGroupIds'] = self.security_groups_ids

        if self.user_options['volume'] != '':
            volume_size = int(self.user_options['volume'])
            run_args['BlockDeviceMappings'] = [{
                'DeviceName': self.__get_root_volume_name(ami),
                'Ebs': {
                    'VolumeSize': volume_size,
                    'VolumeType': 'gp2',
                    'DeleteOnTermination': True
                }
            }]
        instance = ec2_client.run_instances(**run_args)
        self.log.info('Starting EC2 instance')
        instance_id = instance['Instances'][0]['InstanceId']
        waiter = ec2_client.get_waiter('instance_running')
        waiter.wait(InstanceIds=[instance_id])
        if self.use_public_ip is True:
            self.ip = ec2_client.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]['PublicIpAddress']
        else:
            self.ip = ec2_client.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]['PrivateIpAddress']
        self.state.append('Instance running')
        self.log.info('EC2 instance is running (id: {0})'.format(instance_id))
        return instance_id

    def __get_root_volume_name(self, ami):
        ec2_client = boto3.client('ec2', region_name=self.user_options['region'])
        r = ec2_client.describe_images(ImageIds=[ami])
        return r['Images'][0]['BlockDeviceMappings'][0]['DeviceName']

    def __request_spot_instance(self, ami, type, region):
            ec2_client = boto3.client('ec2', region_name=region)
            self.log.info('Requesting spot instance')
            run_args = {
                    'ImageId': ami,
                    'InstanceType': type,
                    'UserData':base64.b64encode(self.USER_DATA_SCRIPT.format(self.ecs_cluster).encode()).decode(),
                    'IamInstanceProfile': {"Arn":self.instance_role_arn},
            }
            if self.key_pair_name:
                run_args['KeyName'] = self.key_pair_name
            if self.subnet_id:
                run_args['SubnetId'] = self.subnet_id
            if self.security_groups_ids:
                run_args['SecurityGroupIds'] = self.security_groups_ids
            if self.user_options['volume'] != '':
                volume_size = int(self.user_options['volume'])
                run_args['BlockDeviceMappings'] = [{
                    'DeviceName': self.__get_root_volume_name(ami),
                    'Ebs': {
                        'VolumeSize': volume_size,
                        'VolumeType': 'gp2',
                        'DeleteOnTermination': True
                    }
                }]
            spot_request = ec2_client.request_spot_instances(
                InstanceCount=1,
                LaunchSpecification=run_args
            )
            self.state.append('Spot request created')
            waiter = ec2_client.get_waiter('spot_instance_request_fulfilled')
            waiter.wait(SpotInstanceRequestIds=[spot_request['SpotInstanceRequests'][0]['SpotInstanceRequestId']])
            self.state.append('Spot instance running')
            spot_instance = ec2_client.describe_spot_instance_requests(SpotInstanceRequestIds=[spot_request['SpotInstanceRequests'][0]['SpotInstanceRequestId']])
            instance_id =  spot_instance['SpotInstanceRequests'][0]['InstanceId']
            self.log.info('Spot instance is running (id: {0})'.format(instance_id))
            ec2_client.create_tags(Resources=[instance_id], Tags=[{'Key': 'Name', 'Value': 'jupyter-notebook-{0}'.format(self.user.name) }])
            if self.use_public_ip is True:
                self.ip = ec2_client.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]['PublicIpAddress']
            else:
                self.ip = ec2_client.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]['PrivateIpAddress']
            return instance_id

    async def __spawn_ec2(self, type):
        region = self.user_options['region']
        if self.instances[region][type].get('gpu'):
            if self.ec2_gpu_ami != '':
                ami = self.ec2_gpu_ami
            else:
                ami = self.amis[region]['gpu']
        elif self.instances[region][type]['arch'] == 'x86_64' or self.instances[region][type]['arch'] == 'i386':
            if self.ec2_ami != '':
                ami = self.ec2_ami
            else:
                ami = self.amis[region]['amd']
        else:
            if self.ec2_arm_ami != '':
                ami = self.ec2_arm_ami
            else:
                ami = self.amis[region]['arm64']
        
        self.log.info('Using AMI {0}'.format(ami))
        if self.user_options.get('spot') is None:
            self.state.append('Requesting {0} non spot instance'.format(type))
            spawn_method = self.__run_instance
        else:
            self.state.append('Requesting {0} spot instance'.format(type))
            spawn_method = self.__request_spot_instance
        with ThreadPoolExecutor(1) as executor:
                future = asyncio.wrap_future(executor.submit(spawn_method, ami, type, region))
                await asyncio.wrap_future(future)
                self.instance_id = future.result()
        self.log.info('Finished spawning EC2')

    def __create_ecs_task(self):
        region = self.user_options['region']
        ecs_client = boto3.client('ecs', region_name=region)
        max_tries = 50
        available_memory = 0
        available_cpu = 0
        self.state.append('Waiting for instance to appear in ECS cluster')
        while True:
            # the wait is a bit hacky, and may break if using a very large image
            if max_tries == 0:
                break
            container_instances_arn = ecs_client.list_container_instances(cluster=self.ecs_cluster)['containerInstanceArns']
            if len(container_instances_arn) == 0:
                max_tries -= 1
                time.sleep(1)
                continue
            container_instances = ecs_client.describe_container_instances(cluster=self.ecs_cluster, containerInstances=container_instances_arn)['containerInstances']
            for container_instance in container_instances:
                if container_instance['ec2InstanceId'] == self.instance_id:
                    self.container_instance_arn = container_instance['containerInstanceArn']
                    for res in container_instance['remainingResources']:
                        if res['name'] == 'CPU':
                            available_cpu = res['integerValue']
                        if res['name'] == 'MEMORY':
                            available_memory = res['integerValue']
                    break
            else:
                max_tries -= 1
                time.sleep(1)
                continue
            break
        if max_tries == 0:
            self.log.warn('Did not find container instance for {0}'.format(self.instance_id))
            return None
        else:
            self.log.info('Found container instance for {0}'.format(self.instance_id))
        ecs_client.put_attributes(cluster=self.ecs_cluster, attributes=[{
            'name': 'jupyter-owner',
            'value': self.user.name,
            'targetId': self.container_instance_arn
        }])

        if self.user_options['image'] != '':
            container_image = self.user_options['image']
        else:
            if self.instances[region][self.user_options['instance']].get('gpu'):
                container_image = self.default_docker_image_gpu
            else:
                container_image = self.default_docker_image
        container_env = self.get_env()
        #make this configurable ?
        container_env['GRANT_SUDO'] = 'yes'
        container_env['NB_USER'] = self.user.name
        container_env['CHOWN_HOME'] = 'yes'
        container_env['JUPYTER_ENABLE_LAB'] = 'yes'
        for k, v in self.custom_env.items():
            container_env[k] = v

        self.log.info('Using docker image {0}'.format(container_image))
        self.log.info('Creating ECS task')
        self.state.append('Creating ECS task')
        r = ecs_client.register_task_definition(
                family='jupyter-task-{0}'.format(self.user.name),
                networkMode='host',
                containerDefinitions=[
                    {
                        'name': 'jupyter-task-{0}'.format(self.user.name),
                        'image': container_image,
                        'cpu': available_cpu,
                        'memory': available_memory,
                        'environment': [{'name': key, 'value': value} for key, value in container_env.items()],
                        'user': 'root',
                        'workingDirectory': '/home/{0}'.format(self.user.name),
                        'command': ['start-singleuser.sh', '--SingleUserNotebookApp.default_url=/lab'],
                        #'logConfiguration': {
                        #    'logDriver': "awslogs",
                        #    'options': {
                        #        'awslogs-region': region,
                        #        'awslogs-create-group': 'true',
                        #        'awslogs-group': "/jupyterhub/jupyter-logs-{0}".format(self.user.name)
                        #    }
                        #}
                    }
                ]
        )
        self.log.info('ECS task created')
        self.task_definition_arn = r['taskDefinition']['taskDefinitionArn']
        self.log.info('Starting ECS task')
        self.state.append('Starting ECS task')
        r = ecs_client.start_task(
                cluster=self.ecs_cluster,
                containerInstances=[
                    self.container_instance_arn,
                ],
                taskDefinition=self.task_definition_arn,
        )
        waiter = ecs_client.get_waiter('tasks_running')
        try:
            waiter.wait(cluster=self.ecs_cluster, tasks=[r['tasks'][0]['taskArn']])
        except Exception as e:
            self.log.error('Exception while waiting for container to be up : {0}'.format(e))
            return None
        self.log.info('ECS task is running')
        return self.task_definition_arn