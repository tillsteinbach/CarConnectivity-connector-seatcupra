

# CarConnectivity Connector for Cupra Vehicles
[![GitHub sourcecode](https://img.shields.io/badge/Source-GitHub-green)](https://github.com/tillsteinbach/CarConnectivity-connector-cupra/)
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/tillsteinbach/CarConnectivity-connector-cupra)](https://github.com/tillsteinbach/CarConnectivity-connector-cupra/releases/latest)
[![GitHub](https://img.shields.io/github/license/tillsteinbach/CarConnectivity-connector-cupra)](https://github.com/tillsteinbach/CarConnectivity-connector-cupra/blob/master/LICENSE)
[![GitHub issues](https://img.shields.io/github/issues/tillsteinbach/CarConnectivity-connector-cupra)](https://github.com/tillsteinbach/CarConnectivity-connector-cupra/issues)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/carconnectivity-connector-cupra?label=PyPI%20Downloads)](https://pypi.org/project/carconnectivity-connector-cupra/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/carconnectivity-connector-cupra)](https://pypi.org/project/carconnectivity-connector-cupra/)
[![Donate at PayPal](https://img.shields.io/badge/Donate-PayPal-2997d8)](https://www.paypal.com/donate?hosted_button_id=2BVFF5GJ9SXAJ)
[![Sponsor at Github](https://img.shields.io/badge/Sponsor-GitHub-28a745)](https://github.com/sponsors/tillsteinbach)


## Due to lack of access to a Cupra car the development of this conenctor is currently stuck. If you want to help me with access to your account, please contact me!

[CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) is a python API to connect to various car services. This connector enables the integration of cupra vehicles through the MyCupra API. Look at [CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) for other supported brands.

## Configuration
In your carconnectivity.json configuration add a section for the cupra connector like this:
```
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "cupra",
                "config": {
                    "username": "test@test.de",
                    "password": "testpassword123"
                }
            }
        ]
    }
}
```
### Credentials
If you do not want to provide your username or password inside the configuration you have to create a ".netrc" file at the appropriate location (usually this is your home folder):
```
# For MyCupra
machine cupra
login test@test.de
password testpassword123
```
In this case the configuration needs to look like this:
```
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "cupra",
                "config": {
                }
            }
        ]
    }
}
```

You can also provide the location of the netrc file in the configuration.
```
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "cupra",
                "config": {
                    "netrc": "/some/path/on/your/filesystem"
                }
            }
        ]
    }
}
```
The optional S-PIN needed for some commands can be provided in the account section of the netrc:
```
# For MyCupra
machine cupra
login test@test.de
password testpassword123
account 1234
```
