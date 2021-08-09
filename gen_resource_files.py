#!/usr/bin/env python

import json

import boto3

def get_aws_regions():
    '''Return all AWS regions'''
    ec2 = boto3.client('ec2')
    describe_args = {}
    ret = []
    while True:
        describe_result = ec2.describe_regions(**describe_args)
        for region in describe_result['Regions']:
            ret.append(region['RegionName'])
        if 'NextToken' not in describe_result:
            break
        describe_args['NextToken'] = describe_result['NextToken']
    return ret
    

def get_ec2_instance_types(region):
    '''Return all EC2 instance types in a region'''
    ec2 = boto3.client('ec2', region_name=region)
    describe_args = {}
    ret = {}
    while True:
        describe_result = ec2.describe_instance_types(**describe_args)
        for instance_type in describe_result['InstanceTypes']:
            instance = {'vcpu': instance_type['VCpuInfo']['DefaultVCpus'], 'memory': instance_type['MemoryInfo']['SizeInMiB'],
            'arch': instance_type['ProcessorInfo']['SupportedArchitectures'][0]}
            if instance_type.get('GpuInfo'):
                instance['gpu'] = {'count': instance_type['GpuInfo']["Gpus"][0]["Count"], 
                    "memory": instance_type['GpuInfo']['TotalGpuMemoryInMiB'], 
                    'type': instance_type['GpuInfo']["Gpus"][0]["Name"]}
            ret[instance_type['InstanceType']] = instance
        if 'NextToken' not in describe_result:
            break
        describe_args['NextToken'] = describe_result['NextToken']
    return ret

def get_ecs_optimized_ami(region):
    ssm = boto3.client('ssm', region_name=region)
    ret = {}
    for type in ['amd', 'arm64', 'gpu']:
        if type == 'amd':
            r = ssm.get_parameters(Names=['/aws/service/ecs/optimized-ami/amazon-linux-2/recommended'])['Parameters']
        else:
            r = ssm.get_parameters(Names=['/aws/service/ecs/optimized-ami/amazon-linux-2/{0}/recommended'.format(type)])['Parameters']
        try:
            value = json.loads(r[0]['Value'])
        except IndexError:
            print(f"Could not find AMI for {type} in region {region}")
            continue
        ret[type] = value['image_id']
    return ret


regions = get_aws_regions()
amis = {}
instances = {}
for region in regions:
    print(f"Getting data for {region}")
    #print("Available instance types in {}".format(region))
    instances[region] = get_ec2_instance_types(region)
    amis[region] = get_ecs_optimized_ami(region)

with open('ecsspawner/amis.json', 'w') as f:
    json.dump(amis, f)

with open('ecsspawner/instances.json', 'w') as f:
    json.dump(instances, f)

with open('ecsspawner/regions.json', 'w') as f:
    json.dump(regions, f)