
# 0.2.0 (Migrating from mujinplanningclientpy)

The `mujinplanningclient` package has been further split into:

- `mujinzmqclient` (this repo) for purely ZMQ-related utilities, and
- `mujinplanningclient` for the planning methods that use each of these.

If you are writing your own ZMQ client, you may only need this repo.
If you are writing something that makes requests to the planning system,
you will need the `mujinplanningclient` repo.

# 0.1.0 (Migrating from mujincontrollerclientpy)

The package `mujincontrollerclient` was split into `mujinwebstackclient` and `mujinplanningclient`. To migrate, determine which methods are used by your controllerclient instance, and convert to the correct class from either package (or use both).

Classes:
- `BinpickingControllerClient` → `BinpickingPlanningClient`
- `HandEyeCalibrationControllerClient` → `HandEyeCalibrationPlanningClient`
- `RealtimeRobotControllerClient` → `RealtimeRobotPlanningClient`
- `RealtimeITLPlanning3ControllerClient` → `RealtimeITL3PlanningClient`
- `ControllerClientError` → `WebstackClientError`
- `UseControllerClientDecorator` → `UsePlanningClientDecorator` AND/OR `UseWebstackClientDecorator`

Imports/Packages:
- `mujincontrollerclient` → `mujinwebstackclient` AND/OR `mujinplanningclient`
- `controllerclientbase` → `webstackclient`
- `controllerclientraw` → `controllerwebclientraw`
- `binpickingcontrollerclient` → `binpickingplanningclient`
- `planningclient` → `planningclient`
- `realtimerobotclient` → `realtimerobotplanningclient`
- `realtimeitlplanning3client` → `realtimeitl3planningclient`
- `handeyecalibrationcontrollerclient` → `handeyecalibrationplanningclient`

In addition:

- `CheckITLProgramExists` has been removed from `realtimeitlplanning3client`. Use webstackclient's `GetProgram` instead.
