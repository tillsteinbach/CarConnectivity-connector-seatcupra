

# CarConnectivity Connector for Cupra Config Options
The configuration for CarConnectivity is a .json file.
## Cupra Connector Options
These are the valid options for the Cupra Connector
```json
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "cupra", // Definition for the Cupra Connector
                "config": {
                    "log_level": "error", // set the connectos log level
                    "interval": 300, // Interval in which the server is checked in seconds
                    "username": "test@test.de", // Username of your Cupra Account
                    "password": "testpassword123", // Username of your Cupra Account
                    "spin": 1234, //S-Pin used for some special commands like locking/unlocking
                    "netrc": "~/.netr", // netrc file if to be used for passwords
                    "api_log_level": "debug", // Show debug information regarding the API
                    "max_age": 300 //Cache requests to the server vor MAX_AGE seconds
                }
            }
        ],
        "plugins": []
    }
}
```