# NOTE

This fork has deviated substantially and the README below is out of date. It will be updated soon but it will be left up in the meantime for reference. Please go to the original repo for their code


# ECSSpawner (WIP)

The ECSSpawner enables JupyterHub to spawn notebooks running on EC2 instances, in an ECS cluster.

It is designed to be flexible for the end-user:
 - You can choose the region where your instance will run
 - You can choose the instance type
 - You can request spot instances
 - Optionally, the docker image to use. If not provided, it will default to the image configured in the hub config
 - Optionally, the size of the root block volume of the instance. If not provided, it will defaut to the size of the volume of the AMI (30 GiB for the ECS optimized AMIs) 

Each notebook server runs on a dedicated EC2 instance which is destroyed when the notebook server is shutdown, removing the need to manage the instances in the ECS cluster.

## Configuration

| Name | Description | Required | Default |
| --- | --- | --- | --- |
| ECSSpawner.default_docker_image | Name of the docker image to use for non-GPU instance | True | N/A |
| ECSSpawner.default_docker_image_gpu | Name of the docker image to use for GPU instance | True | N/A |
| ECSSpawner.instance_role_arn | ARN of the role for the EC2 instance. | True | |
| ECSSpawner.ecs_cluster | Name of the ECS cluster in which to add the instance | False | default |
| ECSSpawner.key_pair_name | Name of a keypair to add to the instance | False | Not set |
| ECSSpawner.subnet_id | Id of the subnet to place the EC2 instances in. If not provided, will use the default subnet of the VPC | False | Default subnet of the default VPC |
| ECSSpawner.security_group_id | List of Security groups to attach to the EC2 instances. If not provided, will use the default security group of the VPC | False | Default security group of the default VPC |
| ECSSpawner.use_public_ip | Use the public IP of the underlying EC2 instance to access the notebook (it means that a public IP must be auto-assigned to the instance). Mainly intended for cases where jupyterhub itself is not running in AWS. | False | False |
| ECSSpawner.ec2_ami | AMI to use for x86 instances | False | The latest ECS optimized x86 AMI in the region |
| ECSSpawner.ec2_arm_ami | AMI to use for ARM instances | False | The latest ECS optimized ARM AMI in the region (caution: AWS does not provide ARM ECS AMI in all regions) |
| ECSSpawner.ec2_gpu_ami | AMI to use for GPU instances | False | The latest ECS optimized GPU AMI in the region |
| ECSSpawner.custom_env | Dict of custom env vars for the notebook | False | {} |



### Private images

Currently only docker hub public images, and ECR public/private images are supported.

ECR private image support relies on giving the correct permissions to the role attached to the instance with ECSSpawner.instance_role_arn.

## Setup

On your jupyterhub server, run: 

`pip install jupyterhub-ecs-spawner`

As EC2 instances are tied to the lifecycle of a notebook server, the data in them is ephemeral and will be lost when the server is stopped.

As such, it is strongly recommended to setup some kind of persistent storage for the notebooks, such as https://github.com/danielfrg/s3contents

It is also recommended to setup https://github.com/jupyterhub/jupyterhub-idle-culler to cull idle servers to avoid runaway costs.

You will need to increase the timeout for spawning notebook server, as it is very unlikely for the ECS task to be up in less than 60s (the default jupyterhub timeout) 

## Resources file

As it would be impractical to query AWS at runtime to get the list of all available instances in a given region, static files containing the list of instances per region and the id of the ECS AMI are generated with the `gen_resource_file.py` script.

This means:
 - If AWS releases new instance types, they won't be available in the list without a new release
 - Same thing goes for the AMIs, if AWS releases new versions, a new release must be made 


## Example configuration

```
from ecsspawner import ECSSpawner

c.JupyterHub.spawner_class = 'ecsspawner.ECSSpawner'


c.ECSSpawner.ecs_cluster = 'test-jupyter'

c.ECSSpawner.default_docker_image = 'jupyter/datascience-notebook:notebook-6.4.0'
c.ECSSpawner.default_docker_image_gpu = 'cschranz/gpu-jupyter:latest'
c.ECSSpawner.instance_role_arn = 'arn:aws:iam::XXXX:instance-profile/ecsInstanceRole'

c.Spawner.start_timeout = 180
```


## TODO
 - Log configuration (allow to use cloudwatch to get the log of the containers)
 - Support docker hub private images
 - Have a more robust system when waiting for the ECS task to be up
