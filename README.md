

# CarConnectivity Connector for Seat and Cupra Vehicles
[![GitHub sourcecode](https://img.shields.io/badge/Source-GitHub-green)](https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/)
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/tillsteinbach/CarConnectivity-connector-seatcupra)](https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/latest)
[![GitHub](https://img.shields.io/github/license/tillsteinbach/CarConnectivity-connector-seatcupra)](https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/blob/master/LICENSE)
[![GitHub issues](https://img.shields.io/github/issues/tillsteinbach/CarConnectivity-connector-seatcupra)](https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/issues)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/carconnectivity-connector-seatcupra?label=PyPI%20Downloads)](https://pypi.org/project/carconnectivity-connector-seatcupra/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/carconnectivity-connector-seatcupra)](https://pypi.org/project/carconnectivity-connector-seatcupra/)
[![Donate at PayPal](https://img.shields.io/badge/Donate-PayPal-2997d8)](https://www.paypal.com/donate?hosted_button_id=2BVFF5GJ9SXAJ)
[![Sponsor at Github](https://img.shields.io/badge/Sponsor-GitHub-28a745)](https://github.com/sponsors/tillsteinbach)

[CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) is a python API to connect to various car services. This connector enables the integration of seat and cupra vehicles through the MyCupra API. Look at [CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) for other supported brands.

## Configuration
In your carconnectivity.json configuration add a section for the seatcupra connector like this:
```
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "seatcupra",
                "config": {
                    "brand": "cupra",
                    "username": "test@test.de",
                    "password": "testpassword123"
                }
            }
        ]
    }
}
```
`brand` (`seat` or `cupra`) defines what login is used. MyCupra or MySeat account. Your credentials will work with both, but you may need to consent again to the terms and conditions when you use the "wrong" brand.

### Credentials
If you do not want to provide your username or password inside the configuration you have to create a ".netrc" file at the appropriate location (usually this is your home folder):
```
# For MyCupra
machine seatcupra
login test@test.de
password testpassword123
```
In this case the configuration needs to look like this:
```
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "seatcupra",
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
                "type": "seatcupra",
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
machine seatcupra
login test@test.de
password testpassword123
account 1234
```
