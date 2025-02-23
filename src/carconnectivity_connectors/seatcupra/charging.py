"""
Module for charging for Seat/Cupra vehicles.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from enum import Enum

from carconnectivity.charging import Charging
from carconnectivity.vehicle import ElectricVehicle

if TYPE_CHECKING:
    from typing import Optional, Dict


class SeatCupraCharging(Charging):  # pylint: disable=too-many-instance-attributes
    """
    SeatCupraCharging class for handling SeatCupra vehicle charging information.

    This class extends the Charging class and includes an enumeration of various
    charging states specific to SeatCupra vehicles.
    """
    def __init__(self, vehicle: ElectricVehicle | None = None, origin: Optional[Charging] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vehicle=vehicle)

    class SeatCupraChargingState(Enum,):
        """
        Enum representing the various charging states for a SeatCupra vehicle.
        """
        OFF = 'off'
        READY_FOR_CHARGING = 'readyForCharging'
        NOT_READY_FOR_CHARGING = 'notReadyForCharging'
        CONSERVATION = 'conservation'
        CHARGE_PURPOSE_REACHED_NOT_CONSERVATION_CHARGING = 'chargePurposeReachedAndNotConservationCharging'
        CHARGE_PURPOSE_REACHED_CONSERVATION = 'chargePurposeReachedAndConservation'
        CHARGING = 'charging'
        ERROR = 'error'
        UNSUPPORTED = 'unsupported'
        DISCHARGING = 'discharging'
        UNKNOWN = 'unknown charging state'

    class SeatCupraChargeMode(Enum,):
        """
        Enum class representing different SeatCupra charge modes.
        """
        MANUAL = 'manual'
        INVALID = 'invalid'
        OFF = 'off'
        TIMER = 'timer'
        ONLY_OWN_CURRENT = 'onlyOwnCurrent'
        PREFERRED_CHARGING_TIMES = 'preferredChargingTimes'
        TIMER_CHARGING_WITH_CLIMATISATION = 'timerChargingWithClimatisation'
        HOME_STORAGE_CHARGING = 'homeStorageCharging'
        IMMEDIATE_DISCHARGING = 'immediateDischarging'
        UNKNOWN = 'unknown charge mode'


# Mapping of Cupra charging states to generic charging states
mapping_seatcupra_charging_state: Dict[SeatCupraCharging.SeatCupraChargingState, Charging.ChargingState] = {
    SeatCupraCharging.SeatCupraChargingState.OFF: Charging.ChargingState.OFF,
    SeatCupraCharging.SeatCupraChargingState.NOT_READY_FOR_CHARGING: Charging.ChargingState.OFF,
    SeatCupraCharging.SeatCupraChargingState.READY_FOR_CHARGING: Charging.ChargingState.READY_FOR_CHARGING,
    SeatCupraCharging.SeatCupraChargingState.CONSERVATION: Charging.ChargingState.CONSERVATION,
    SeatCupraCharging.SeatCupraChargingState.CHARGE_PURPOSE_REACHED_NOT_CONSERVATION_CHARGING: Charging.ChargingState.READY_FOR_CHARGING,
    SeatCupraCharging.SeatCupraChargingState.CHARGE_PURPOSE_REACHED_CONSERVATION: Charging.ChargingState.CONSERVATION,
    SeatCupraCharging.SeatCupraChargingState.CHARGING: Charging.ChargingState.CHARGING,
    SeatCupraCharging.SeatCupraChargingState.ERROR: Charging.ChargingState.ERROR,
    SeatCupraCharging.SeatCupraChargingState.UNSUPPORTED: Charging.ChargingState.UNSUPPORTED,
    SeatCupraCharging.SeatCupraChargingState.DISCHARGING: Charging.ChargingState.DISCHARGING,
    SeatCupraCharging.SeatCupraChargingState.UNKNOWN: Charging.ChargingState.UNKNOWN
}
