"""Support for the Fitbit API."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import datetime
import logging
import os
from typing import Any, Final, cast

import voluptuous as vol

from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as PARENT_PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_TOKEN,
    CONF_UNIT_SYSTEM,
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfMass,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.icon import icon_for_battery_level
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util.json import load_json_object

from .api import FitbitApi
from .const import (
    ATTR_ACCESS_TOKEN,
    ATTR_LAST_SAVED_AT,
    ATTR_REFRESH_TOKEN,
    ATTRIBUTION,
    BATTERY_LEVELS,
    CONF_CLOCK_FORMAT,
    CONF_MONITORED_RESOURCES,
    DEFAULT_CLOCK_FORMAT,
    DEFAULT_CONFIG,
    DOMAIN,
    FITBIT_CONFIG_FILE,
    FITBIT_DEFAULT_RESOURCES,
    FitbitUnitSystem,
)
from .model import FitbitDevice

_LOGGER: Final = logging.getLogger(__name__)

_CONFIGURING: dict[str, str] = {}

SCAN_INTERVAL: Final = datetime.timedelta(minutes=30)


def _default_value_fn(result: dict[str, Any]) -> str:
    """Parse a Fitbit timeseries API responses."""
    return cast(str, result["value"])


def _distance_value_fn(result: dict[str, Any]) -> int | str:
    """Format function for distance values."""
    return format(float(_default_value_fn(result)), ".2f")


def _body_value_fn(result: dict[str, Any]) -> int | str:
    """Format function for body values."""
    return format(float(_default_value_fn(result)), ".1f")


def _clock_format_12h(result: dict[str, Any]) -> str:
    raw_state = result["value"]
    if raw_state == "":
        return "-"
    hours_str, minutes_str = raw_state.split(":")
    hours, minutes = int(hours_str), int(minutes_str)
    setting = "AM"
    if hours > 12:
        setting = "PM"
        hours -= 12
    elif hours == 0:
        hours = 12
    return f"{hours}:{minutes:02d} {setting}"


def _weight_unit(unit_system: FitbitUnitSystem) -> UnitOfMass:
    """Determine the weight unit."""
    if unit_system == FitbitUnitSystem.EN_US:
        return UnitOfMass.POUNDS
    if unit_system == FitbitUnitSystem.EN_GB:
        return UnitOfMass.STONES
    return UnitOfMass.KILOGRAMS


def _distance_unit(unit_system: FitbitUnitSystem) -> UnitOfLength:
    """Determine the distance unit."""
    if unit_system == FitbitUnitSystem.EN_US:
        return UnitOfLength.MILES
    return UnitOfLength.KILOMETERS


def _elevation_unit(unit_system: FitbitUnitSystem) -> UnitOfLength:
    """Determine the elevation unit."""
    if unit_system == FitbitUnitSystem.EN_US:
        return UnitOfLength.FEET
    return UnitOfLength.METERS


@dataclass
class FitbitSensorEntityDescription(SensorEntityDescription):
    """Describes Fitbit sensor entity."""

    unit_type: str | None = None
    value_fn: Callable[[dict[str, Any]], Any] = _default_value_fn
    unit_fn: Callable[[FitbitUnitSystem], str | None] = lambda x: None
    scope: str | None = None


FITBIT_RESOURCES_LIST: Final[tuple[FitbitSensorEntityDescription, ...]] = (
    FitbitSensorEntityDescription(
        key="activities/activityCalories",
        name="Activity Calories",
        native_unit_of_measurement="cal",
        icon="mdi:fire",
        scope="activity",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/calories",
        name="Calories",
        native_unit_of_measurement="cal",
        icon="mdi:fire",
        scope="activity",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    FitbitSensorEntityDescription(
        key="activities/caloriesBMR",
        name="Calories BMR",
        native_unit_of_measurement="cal",
        icon="mdi:fire",
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/distance",
        name="Distance",
        icon="mdi:map-marker",
        device_class=SensorDeviceClass.DISTANCE,
        value_fn=_distance_value_fn,
        unit_fn=_distance_unit,
        scope="activity",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    FitbitSensorEntityDescription(
        key="activities/elevation",
        name="Elevation",
        icon="mdi:walk",
        device_class=SensorDeviceClass.DISTANCE,
        unit_fn=_elevation_unit,
        scope="activity",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/floors",
        name="Floors",
        native_unit_of_measurement="floors",
        icon="mdi:walk",
        scope="activity",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/heart",
        name="Resting Heart Rate",
        native_unit_of_measurement="bpm",
        icon="mdi:heart-pulse",
        value_fn=lambda result: int(result["value"]["restingHeartRate"]),
        scope="heartrate",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    FitbitSensorEntityDescription(
        key="activities/minutesFairlyActive",
        name="Minutes Fairly Active",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:walk",
        device_class=SensorDeviceClass.DURATION,
        scope="activity",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/minutesLightlyActive",
        name="Minutes Lightly Active",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:walk",
        device_class=SensorDeviceClass.DURATION,
        scope="activity",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/minutesSedentary",
        name="Minutes Sedentary",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:seat-recline-normal",
        device_class=SensorDeviceClass.DURATION,
        scope="activity",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/minutesVeryActive",
        name="Minutes Very Active",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:run",
        device_class=SensorDeviceClass.DURATION,
        scope="activity",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/steps",
        name="Steps",
        native_unit_of_measurement="steps",
        icon="mdi:walk",
        scope="activity",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    FitbitSensorEntityDescription(
        key="activities/tracker/activityCalories",
        name="Tracker Activity Calories",
        native_unit_of_measurement="cal",
        icon="mdi:fire",
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/tracker/calories",
        name="Tracker Calories",
        native_unit_of_measurement="cal",
        icon="mdi:fire",
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/tracker/distance",
        name="Tracker Distance",
        icon="mdi:map-marker",
        device_class=SensorDeviceClass.DISTANCE,
        value_fn=_distance_value_fn,
        unit_fn=_distance_unit,
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/tracker/elevation",
        name="Tracker Elevation",
        icon="mdi:walk",
        device_class=SensorDeviceClass.DISTANCE,
        unit_fn=_elevation_unit,
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/tracker/floors",
        name="Tracker Floors",
        native_unit_of_measurement="floors",
        icon="mdi:walk",
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/tracker/minutesFairlyActive",
        name="Tracker Minutes Fairly Active",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:walk",
        device_class=SensorDeviceClass.DURATION,
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/tracker/minutesLightlyActive",
        name="Tracker Minutes Lightly Active",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:walk",
        device_class=SensorDeviceClass.DURATION,
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/tracker/minutesSedentary",
        name="Tracker Minutes Sedentary",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:seat-recline-normal",
        device_class=SensorDeviceClass.DURATION,
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/tracker/minutesVeryActive",
        name="Tracker Minutes Very Active",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:run",
        device_class=SensorDeviceClass.DURATION,
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="activities/tracker/steps",
        name="Tracker Steps",
        native_unit_of_measurement="steps",
        icon="mdi:walk",
        scope="activity",
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="body/bmi",
        name="BMI",
        native_unit_of_measurement="BMI",
        icon="mdi:human",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_body_value_fn,
        scope="weight",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="body/fat",
        name="Body Fat",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:human",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_body_value_fn,
        scope="weight",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="body/weight",
        name="Weight",
        icon="mdi:human",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.WEIGHT,
        value_fn=_body_value_fn,
        unit_fn=_weight_unit,
        scope="weight",
    ),
    FitbitSensorEntityDescription(
        key="sleep/awakeningsCount",
        name="Awakenings Count",
        native_unit_of_measurement="times awaken",
        icon="mdi:sleep",
        scope="sleep",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="sleep/efficiency",
        name="Sleep Efficiency",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:sleep",
        state_class=SensorStateClass.MEASUREMENT,
        scope="sleep",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="sleep/minutesAfterWakeup",
        name="Minutes After Wakeup",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:sleep",
        device_class=SensorDeviceClass.DURATION,
        scope="sleep",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="sleep/minutesAsleep",
        name="Sleep Minutes Asleep",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:sleep",
        device_class=SensorDeviceClass.DURATION,
        scope="sleep",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="sleep/minutesAwake",
        name="Sleep Minutes Awake",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:sleep",
        device_class=SensorDeviceClass.DURATION,
        scope="sleep",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="sleep/minutesToFallAsleep",
        name="Sleep Minutes to Fall Asleep",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:sleep",
        device_class=SensorDeviceClass.DURATION,
        scope="sleep",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FitbitSensorEntityDescription(
        key="sleep/timeInBed",
        name="Sleep Time in Bed",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:hotel",
        device_class=SensorDeviceClass.DURATION,
        scope="sleep",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

# Different description depending on clock format
SLEEP_START_TIME = FitbitSensorEntityDescription(
    key="sleep/startTime",
    name="Sleep Start Time",
    icon="mdi:clock",
    scope="sleep",
    entity_category=EntityCategory.DIAGNOSTIC,
)
SLEEP_START_TIME_12HR = FitbitSensorEntityDescription(
    key="sleep/startTime",
    name="Sleep Start Time",
    icon="mdi:clock",
    value_fn=_clock_format_12h,
    scope="sleep",
    entity_category=EntityCategory.DIAGNOSTIC,
)

FITBIT_RESOURCE_BATTERY = FitbitSensorEntityDescription(
    key="devices/battery",
    name="Battery",
    icon="mdi:battery",
    scope="settings",
    entity_category=EntityCategory.DIAGNOSTIC,
)

FITBIT_RESOURCES_KEYS: Final[list[str]] = [
    desc.key
    for desc in (*FITBIT_RESOURCES_LIST, FITBIT_RESOURCE_BATTERY, SLEEP_START_TIME)
]

PLATFORM_SCHEMA: Final = PARENT_PLATFORM_SCHEMA.extend(
    {
        vol.Optional(
            CONF_MONITORED_RESOURCES, default=FITBIT_DEFAULT_RESOURCES
        ): vol.All(cv.ensure_list, [vol.In(FITBIT_RESOURCES_KEYS)]),
        vol.Optional(CONF_CLOCK_FORMAT, default=DEFAULT_CLOCK_FORMAT): vol.In(
            ["12H", "24H"]
        ),
        vol.Optional(CONF_UNIT_SYSTEM, default=FitbitUnitSystem.LEGACY_DEFAULT): vol.In(
            [
                FitbitUnitSystem.EN_GB,
                FitbitUnitSystem.EN_US,
                FitbitUnitSystem.METRIC,
                FitbitUnitSystem.LEGACY_DEFAULT,
            ]
        ),
    }
)

# Only import configuration if it was previously created successfully with all
# of the following fields.
FITBIT_CONF_KEYS = [
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    ATTR_ACCESS_TOKEN,
    ATTR_REFRESH_TOKEN,
    ATTR_LAST_SAVED_AT,
]


def load_config_file(config_path: str) -> dict[str, Any] | None:
    """Load existing valid fitbit.conf from disk for import."""
    if os.path.isfile(config_path):
        config_file = load_json_object(config_path)
        if config_file != DEFAULT_CONFIG and all(
            key in config_file for key in FITBIT_CONF_KEYS
        ):
            return config_file
    return None


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Fitbit sensor."""
    config_path = hass.config.path(FITBIT_CONFIG_FILE)
    config_file = await hass.async_add_executor_job(load_config_file, config_path)
    _LOGGER.debug("loaded config file: %s", config_file)

    if config_file is not None:
        _LOGGER.debug("Importing existing fitbit.conf application credentials")
        await async_import_client_credential(
            hass,
            DOMAIN,
            ClientCredential(
                config_file[CONF_CLIENT_ID], config_file[CONF_CLIENT_SECRET]
            ),
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                "auth_implementation": DOMAIN,
                CONF_TOKEN: {
                    ATTR_ACCESS_TOKEN: config_file[ATTR_ACCESS_TOKEN],
                    ATTR_REFRESH_TOKEN: config_file[ATTR_REFRESH_TOKEN],
                    "expires_at": config_file[ATTR_LAST_SAVED_AT],
                },
                CONF_CLOCK_FORMAT: config[CONF_CLOCK_FORMAT],
                CONF_UNIT_SYSTEM: config[CONF_UNIT_SYSTEM],
                CONF_MONITORED_RESOURCES: config[CONF_MONITORED_RESOURCES],
            },
        )
        translation_key = "deprecated_yaml_import"
        if (
            result.get("type") == FlowResultType.ABORT
            and result.get("reason") == "cannot_connect"
        ):
            translation_key = "deprecated_yaml_import_issue_cannot_connect"
    else:
        translation_key = "deprecated_yaml_no_import"

    async_create_issue(
        hass,
        DOMAIN,
        "deprecated_yaml",
        breaks_in_ha_version="2024.5.0",
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        translation_key=translation_key,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Fitbit sensor platform."""

    api: FitbitApi = hass.data[DOMAIN][entry.entry_id]

    # Note: This will only be one rpc since it will cache the user profile
    (user_profile, unit_system) = await asyncio.gather(
        api.async_get_user_profile(), api.async_get_unit_system()
    )

    clock_format = entry.data.get(CONF_CLOCK_FORMAT)

    # Originally entities were configured explicitly from yaml config. Newer
    # configurations will infer which entities to enable based on the allowed
    # scopes the user selected during OAuth. When creating entities based on
    # scopes, some entities are disabled by default.
    monitored_resources = entry.data.get(CONF_MONITORED_RESOURCES)
    scopes = entry.data["token"].get("scope", "").split(" ")

    def is_explicit_enable(description: FitbitSensorEntityDescription) -> bool:
        """Determine if entity is enabled by default."""
        if monitored_resources is not None:
            return description.key in monitored_resources
        return False

    def is_allowed_resource(description: FitbitSensorEntityDescription) -> bool:
        """Determine if an entity is allowed to be created."""
        if is_explicit_enable(description):
            return True
        return description.scope in scopes

    resource_list = [
        *FITBIT_RESOURCES_LIST,
        SLEEP_START_TIME_12HR if clock_format == "12H" else SLEEP_START_TIME,
    ]

    entities = [
        FitbitSensor(
            api,
            user_profile.encoded_id,
            description,
            units=description.unit_fn(unit_system),
            enable_default_override=is_explicit_enable(description),
        )
        for description in resource_list
        if is_allowed_resource(description)
    ]
    if is_allowed_resource(FITBIT_RESOURCE_BATTERY):
        devices = await api.async_get_devices()
        entities.extend(
            [
                FitbitSensor(
                    api,
                    user_profile.encoded_id,
                    FITBIT_RESOURCE_BATTERY,
                    device=device,
                    enable_default_override=is_explicit_enable(FITBIT_RESOURCE_BATTERY),
                )
                for device in devices
            ]
        )
    async_add_entities(entities, True)


class FitbitSensor(SensorEntity):
    """Implementation of a Fitbit sensor."""

    entity_description: FitbitSensorEntityDescription
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        api: FitbitApi,
        user_profile_id: str,
        description: FitbitSensorEntityDescription,
        device: FitbitDevice | None = None,
        units: str | None = None,
        enable_default_override: bool = False,
    ) -> None:
        """Initialize the Fitbit sensor."""
        self.entity_description = description
        self.api = api
        self.device = device

        self._attr_unique_id = f"{user_profile_id}_{description.key}"
        if device is not None:
            self._attr_name = f"{device.device_version} Battery"
            self._attr_unique_id = f"{self._attr_unique_id}_{device.id}"

        if units is not None:
            self._attr_native_unit_of_measurement = units

        if enable_default_override:
            self._attr_entity_registry_enabled_default = True

    @property
    def icon(self) -> str | None:
        """Icon to use in the frontend, if any."""
        if (
            self.entity_description.key == "devices/battery"
            and self.device is not None
            and (battery_level := BATTERY_LEVELS.get(self.device.battery)) is not None
        ):
            return icon_for_battery_level(battery_level=battery_level)
        return self.entity_description.icon

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return the state attributes."""
        attrs: dict[str, str | None] = {}

        if self.device is not None:
            attrs["model"] = self.device.device_version
            device_type = self.device.type
            attrs["type"] = device_type.lower() if device_type is not None else None

        return attrs

    async def async_update(self) -> None:
        """Get the latest data from the Fitbit API and update the states."""
        resource_type = self.entity_description.key
        if resource_type == "devices/battery" and self.device is not None:
            device_id = self.device.id
            registered_devs: list[FitbitDevice] = await self.api.async_get_devices()
            self.device = next(
                device for device in registered_devs if device.id == device_id
            )
            self._attr_native_value = self.device.battery

        else:
            result = await self.api.async_get_latest_time_series(resource_type)
            self._attr_native_value = self.entity_description.value_fn(result)
