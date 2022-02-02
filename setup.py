import setuptools

setuptools.setup(
    name='jupyterhub-ecs-spawner',
    version='0.0.1',
    author='Sebastien Blot',
    author_email='sebastien@crowdsec.net',
    description='Spawn Jupyter notebook running on dedicated EC2 instance in an ECS cluster',
    packages=setuptools.find_packages(),
    package_data={"": ["form_template.html", "instances.json", "amis.json", "regions.json"]},
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ]
)