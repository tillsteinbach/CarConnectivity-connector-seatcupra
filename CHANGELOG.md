# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]
- No unreleased changes so far

## [0.4.7] - 2025-11-02
### Changed
- Updated some dependencies

## [0.4.6] - 2025-08-31
### Fixed
Fixes 404 error when fetching capabilities

## [0.4.5] - 2025-06-26
### Fixed
Fixes crash when image from server is broken

## [0.4.4] - 2025-06-20
### Fixed
reupload to pypi

## [0.4.3] - 2025-06-20
### Fixed
- Fixes bug that registers hooks several times, causing multiple calls to the servers

### Changed
- Updated dependencies

## [0.4.2] - 2025-04-19
### Fixed
- Fix for problems introduced with PyJWT

## [0.4.1] - 2025-04-19
### Changed
- Use PyJWT instead of jwt

## [0.4] - 2025-04-17
### Fixed
- Bug in mode attribute that caused the connector to crash

### Changed
- Updated dependencies
- stripping of leading and trailing spaces in commands

### Added
- Precision for all attributes is now used when displaying values

## [0.3] - 2025-04-02
### Fixed
- Problem where the connector reported an error on commands that executed successfully
- Allowes to have multiple instances of this connector running

### Changed
- Updated dependencies

## [0.2] - 2025-03-20
### Added
- Support for window heating attributes
- Support for window heating command
- Support for changing charging settings
- Support for adblue range

## [0.1.2] - 2025-03-07
### Fixed
- Fixed bug during refreshing tokens due to connection being reset on server side and client session timing out.

## [0.1.1] - 2025-03-04
### Fixed
- Fixed potential http error when parking position was fetched but due to error not available

### Added
- Added connection state and vehicle state to the public API

## [0.1] - 2025-03-02
Initial release, let's go and give this to the public to try out...

[unreleased]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/compare/v0.4.7...HEAD
[0.4.7]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.4.7
[0.4.6]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.4.6
[0.4.5]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.4.5
[0.4.4]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.4.4
[0.4.3]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.4.3
[0.4.2]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.4.2
[0.4.1]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.4.1
[0.4]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.4
[0.3]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.3
[0.2]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.2
[0.1.2]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.1.2
[0.1.1]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.1.1
[0.1]: https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra/releases/tag/v0.1
