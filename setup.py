from setuptools import setup

install_requires = [
    'fusepy',
    'gmusicapi',
    'oauth2client',
    'eyed3'
]

setup(
    name='GMusicFS',
    version='0.1',
    description='A FUSE filesystem for Google Music',
    author='Ryan McGuire',
    author_email='ryan@enigmacurry.com',
    url='http://github.com/EnigmaCurry/GMusicFS',
    license='MIT',
    install_requires=install_requires,
    zip_safe=False,
    packages=['gmusicfs'],
    entry_points={
        'console_scripts': ['gmusicfs=gmusicfs.gmusicfs:main']},
)
