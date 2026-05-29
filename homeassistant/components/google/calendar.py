"""Support for Google Calendar Search binary sensors."""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses
from datetime import datetime, timedelta
import logging
from typing import Any, cast

import voluptuous as vol

from gcal_sync.api import Range, SyncEventsRequest
from gcal_sync.exceptions import ApiException
from gcal_sync.model import (
    AccessRole,
    Attendee,
    Calendar,
    ColorDefinition,
    DateOrDatetime,
    Event,
    EventTypeEnum,
    ResponseStatus,
)
from gcal_sync.store import ScopedCalendarStore
from gcal_sync.sync import CalendarEventSyncManager

from homeassistant.components.calendar import (
    ENTITY_ID_FORMAT,
    EVENT_DESCRIPTION,
    EVENT_END,
    EVENT_LOCATION,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
    EVENT_TYPES,
    CalendarEntity,
    CalendarEntityDescription,
    CalendarEntityFeature,
    CalendarEvent,
    MIN_NEW_EVENT_DURATION,
    _as_local_timezone,
    _has_consistent_timezone,
    _has_min_duration,
    extract_offset,
    is_offset_reached,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_ENTITIES, CONF_NAME, CONF_OFFSET
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, PlatformNotReady
from homeassistant.helpers import config_validation as cv, entity_platform, entity_registry as er
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import (
    CONF_IGNORE_AVAILABILITY,
    CONF_SEARCH,
    CONF_TRACK,
    DEFAULT_CONF_OFFSET,
    YAML_DEVICES,
    get_calendar_info,
    load_config,
    update_config,
)
from .api import get_feature_access
from .const import (
    EVENT_END_DATE,
    EVENT_END_DATETIME,
    EVENT_IN,
    EVENT_IN_DAYS,
    EVENT_IN_WEEKS,
    EVENT_START_DATE,
    EVENT_START_DATETIME,
    FeatureAccess,
)
from .coordinator import CalendarQueryUpdateCoordinator, CalendarSyncUpdateCoordinator
from .store import GoogleConfigEntry

_LOGGER = logging.getLogger(__name__)

EVENT_ATTENDEES = "attendees"

GOOGLE_CREATE_EVENT_SCHEMA = vol.All(
    cv.has_at_least_one_key(EVENT_START_DATE, EVENT_START_DATETIME, EVENT_IN),
    cv.has_at_most_one_key(EVENT_START_DATE, EVENT_START_DATETIME, EVENT_IN),
    cv.make_entity_service_schema(
        {
            vol.Required(EVENT_SUMMARY): cv.string,
            vol.Optional(EVENT_DESCRIPTION, default=""): cv.string,
            vol.Optional(EVENT_LOCATION): cv.string,
            vol.Optional(EVENT_ATTENDEES, default=[]): vol.Any(
                cv.string, vol.All(cv.ensure_list, [cv.string])
            ),
            vol.Inclusive(
                EVENT_START_DATE, "dates", "Start and end dates must both be specified"
            ): cv.date,
            vol.Inclusive(
                EVENT_END_DATE, "dates", "Start and end dates must both be specified"
            ): cv.date,
            vol.Inclusive(
                EVENT_START_DATETIME,
                "datetimes",
                "Start and end datetimes must both be specified",
            ): cv.datetime,
            vol.Inclusive(
                EVENT_END_DATETIME,
                "datetimes",
                "Start and end datetimes must both be specified",
            ): cv.datetime,
            vol.Optional(EVENT_IN): vol.Schema(
                {
                    vol.Exclusive(EVENT_IN_DAYS, EVENT_TYPES): cv.positive_int,
                    vol.Exclusive(EVENT_IN_WEEKS, EVENT_TYPES): cv.positive_int,
                }
            ),
        },
    ),
    _has_consistent_timezone(EVENT_START_DATETIME, EVENT_END_DATETIME),
    _as_local_timezone(EVENT_START_DATETIME, EVENT_END_DATETIME),
    _has_min_duration(EVENT_START_DATE, EVENT_END_DATE, MIN_NEW_EVENT_DURATION),
    _has_min_duration(EVENT_START_DATETIME, EVENT_END_DATETIME, MIN_NEW_EVENT_DURATION),
)


def _attendees_from_emails(attendees: list[str] | str) -> list[Attendee]:
    """Build Google attendee models from email strings."""
    if isinstance(attendees, str):
        attendees = [email.strip() for email in attendees.split(",")]
    return [Attendee(email=email) for email in attendees if email]

# Avoid syncing super old data on initial syncs. Note that old but active
# recurring events are still included.
SYNC_EVENT_MIN_TIME = timedelta(days=-90)

# Events have a transparency that determine whether or not they block time on calendar.
# When an event is opaque, it means "Show me as busy" which is the default.  Events that
# are not opaque are ignored by default.
OPAQUE = "opaque"

# Google calendar prefixes recurrence rules with RRULE: which
# we need to strip when working with the frontend recurrence rule values
RRULE_PREFIX = "RRULE:"

SERVICE_CREATE_EVENT = "create_event"
FILTERED_EVENT_TYPES = [EventTypeEnum.BIRTHDAY, EventTypeEnum.WORKING_LOCATION]


@dataclasses.dataclass(frozen=True, kw_only=True)
class GoogleCalendarEntityDescription(CalendarEntityDescription):
    """Google calendar entity description."""

    name: str | None
    entity_id: str | None
    read_only: bool
    ignore_availability: bool
    offset: str | None
    search: str | None
    local_sync: bool
    device_id: str
    foreground_color: str | None = None
    event_type: EventTypeEnum | None = None


def _get_entity_descriptions(
    hass: HomeAssistant,
    config_entry: GoogleConfigEntry,
    calendar_item: Calendar,
    calendar_info: Mapping[str, Any],
) -> list[GoogleCalendarEntityDescription]:
    """Create entity descriptions for the calendar.

    The entity descriptions are based on the type of Calendar from the API
    and optional calendar_info yaml configuration that is the older way to
    configure calendars before they supported UI based config.

    The yaml config may map one calendar to multiple entities and they do not
    have a unique id. The yaml config also supports additional options like
    offsets or search.
    """
    calendar_id = calendar_item.id
    num_entities = len(calendar_info[CONF_ENTITIES])
    entity_descriptions = []
    for data in calendar_info[CONF_ENTITIES]:
        if num_entities > 1:
            key = ""
        else:
            key = calendar_id
        entity_enabled = data.get(CONF_TRACK, True)
        if not entity_enabled:
            _LOGGER.warning(
                "The 'track' option in google_calendars.yaml has been deprecated."
                " The setting has been imported to the UI, and should now be"
                " removed from google_calendars.yaml"
            )
        read_only = not (
            calendar_item.access_role.is_writer
            and get_feature_access(config_entry) is FeatureAccess.read_write
        )
        # Prefer calendar sync down of resources when possible. However,
        # sync does not work for search. Also free-busy calendars denormalize
        # recurring events as individual events which is not efficient for sync
        local_sync = True
        if (
            search := data.get(CONF_SEARCH)
        ) or calendar_item.access_role == AccessRole.FREE_BUSY_READER:
            read_only = True
            local_sync = False
        entity_description = GoogleCalendarEntityDescription(
            key=key,
            name=data[CONF_NAME].capitalize(),
            entity_id=generate_entity_id(
                ENTITY_ID_FORMAT, data[CONF_DEVICE_ID], hass=hass
            ),
            read_only=read_only,
            ignore_availability=data.get(CONF_IGNORE_AVAILABILITY, False),
            offset=data.get(CONF_OFFSET, DEFAULT_CONF_OFFSET),
            search=search,
            local_sync=local_sync,
            entity_registry_enabled_default=entity_enabled,
            device_id=data[CONF_DEVICE_ID],
            initial_color=calendar_item.background_color,
            foreground_color=calendar_item.foreground_color,
        )
        entity_descriptions.append(entity_description)
        _LOGGER.debug(
            "calendar_item.primary=%s, search=%s, calendar_item.access_role=%s - %s",
            calendar_item.primary,
            search,
            calendar_item.access_role,
            local_sync,
        )
        if calendar_item.primary and local_sync:
            # Create a separate calendar for birthdays
            entity_descriptions.append(
                dataclasses.replace(
                    entity_description,
                    key=f"{key}-birthdays",
                    translation_key="birthdays",
                    event_type=EventTypeEnum.BIRTHDAY,
                    name=None,
                    entity_id=None,
                )
            )
            # Create an optional disabled by default entity for Work Location
            entity_descriptions.append(
                dataclasses.replace(
                    entity_description,
                    key=f"{key}-work-location",
                    translation_key="working_location",
                    event_type=EventTypeEnum.WORKING_LOCATION,
                    name=None,
                    entity_id=None,
                    entity_registry_enabled_default=False,
                )
            )
    return entity_descriptions


async def _async_get_event_colors(
    calendar_service: Any,
) -> Mapping[str, ColorDefinition]:
    """Return Google Calendar event color definitions keyed by color id."""
    try:
        colors = await calendar_service.async_get_colors()
    except ApiException:
        _LOGGER.debug("Unable to fetch Google Calendar colors", exc_info=True)
        return {}
    return colors.event


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: GoogleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the google calendar platform."""
    calendar_service = config_entry.runtime_data.service
    store = config_entry.runtime_data.store
    try:
        result = await calendar_service.async_list_calendars()
    except ApiException as err:
        raise PlatformNotReady(str(err)) from err

    event_colors = await _async_get_event_colors(calendar_service)

    entity_registry = er.async_get(hass)
    registry_entries = er.async_entries_for_config_entry(
        entity_registry, config_entry.entry_id
    )
    entity_entry_map = {
        entity_entry.unique_id: entity_entry for entity_entry in registry_entries
    }

    # Yaml configuration may override objects from the API
    calendars = await hass.async_add_executor_job(
        load_config, hass.config.path(YAML_DEVICES)
    )
    new_calendars = []
    entities = []
    for calendar_item in result.items:
        calendar_id = calendar_item.id
        if calendars and calendar_id in calendars:
            calendar_info = calendars[calendar_id]
        else:
            calendar_info = get_calendar_info(
                hass, calendar_item.model_dump(exclude_unset=True)
            )
            new_calendars.append(calendar_info)

        for entity_description in _get_entity_descriptions(
            hass, config_entry, calendar_item, calendar_info
        ):
            unique_id = (
                f"{config_entry.unique_id}-{entity_description.key}"
                if entity_description.key
                else None
            )
            # Migrate to new unique_id format which supports
            # multiple config entries as of 2022.7
            for old_unique_id in (
                calendar_id,
                f"{calendar_id}-{entity_description.device_id}",
            ):
                if not (entity_entry := entity_entry_map.get(old_unique_id)):
                    continue
                if unique_id:
                    _LOGGER.debug(
                        "Migrating unique_id for %s from %s to %s",
                        entity_entry.entity_id,
                        old_unique_id,
                        unique_id,
                    )
                    entity_registry.async_update_entity(
                        entity_entry.entity_id, new_unique_id=unique_id
                    )
                else:
                    _LOGGER.debug(
                        "Removing entity registry entry for %s from %s",
                        entity_entry.entity_id,
                        old_unique_id,
                    )
                    entity_registry.async_remove(
                        entity_entry.entity_id,
                    )
            _LOGGER.debug("Creating entity with unique_id=%s", unique_id)
            coordinator: CalendarSyncUpdateCoordinator | CalendarQueryUpdateCoordinator
            if not entity_description.local_sync:
                coordinator = CalendarQueryUpdateCoordinator(
                    hass,
                    config_entry,
                    calendar_service,
                    entity_description.name or entity_description.key,
                    calendar_id,
                    entity_description.search,
                )
            else:
                request_template = SyncEventsRequest(
                    calendar_id=calendar_id,
                    start_time=dt_util.now() + SYNC_EVENT_MIN_TIME,
                )
                sync = CalendarEventSyncManager(
                    calendar_service,
                    store=ScopedCalendarStore(
                        store, unique_id or entity_description.device_id
                    ),
                    request_template=request_template,
                )
                coordinator = CalendarSyncUpdateCoordinator(
                    hass,
                    config_entry,
                    sync,
                    entity_description.name or entity_description.key,
                )
            entities.append(
                GoogleCalendarEntity(
                    coordinator,
                    calendar_id,
                    entity_description,
                    unique_id,
                    event_colors,
                )
            )

    async_add_entities(entities)

    if calendars and new_calendars:

        def append_calendars_to_config() -> None:
            path = hass.config.path(YAML_DEVICES)
            for calendar in new_calendars:
                update_config(path, calendar)

        await hass.async_add_executor_job(append_calendars_to_config)

    platform = entity_platform.async_get_current_platform()
    if (
        any(calendar_item.access_role.is_writer for calendar_item in result.items)
        and get_feature_access(config_entry) is FeatureAccess.read_write
    ):
        platform.async_register_entity_service(
            SERVICE_CREATE_EVENT,
            GOOGLE_CREATE_EVENT_SCHEMA,
            async_create_event,
            required_features=CalendarEntityFeature.CREATE_EVENT,
        )


class GoogleCalendarEntity(
    CoordinatorEntity[CalendarSyncUpdateCoordinator | CalendarQueryUpdateCoordinator],
    CalendarEntity,
):
    """A calendar event entity."""

    entity_description: GoogleCalendarEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CalendarSyncUpdateCoordinator | CalendarQueryUpdateCoordinator,
        calendar_id: str,
        entity_description: GoogleCalendarEntityDescription,
        unique_id: str | None,
        event_colors: Mapping[str, ColorDefinition],
    ) -> None:
        """Create the Calendar event device."""
        super().__init__(coordinator)
        _LOGGER.debug("entity_description.entity_id=%s", entity_description.entity_id)
        _LOGGER.debug("entity_description=%s", entity_description)
        self.calendar_id = calendar_id
        self.entity_description = entity_description
        self._ignore_availability = entity_description.ignore_availability
        self._offset = entity_description.offset
        self._event_colors = event_colors
        self._event: CalendarEvent | None = None
        if entity_description.entity_id:
            self.entity_id = entity_description.entity_id
        self._attr_unique_id = unique_id
        if not entity_description.read_only:
            self._attr_supported_features = (
                CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.DELETE_EVENT
            )

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        """Return the device state attributes."""
        return {"offset_reached": self.offset_reached}

    @property
    def offset_reached(self) -> bool:
        """Return whether or not the event offset was reached."""
        (event, offset_value) = self._event_with_offset()
        if event is not None and offset_value is not None:
            return is_offset_reached(event.start_datetime_local, offset_value)
        return False

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        (event, _) = self._event_with_offset()
        return event

    def _event_filter(self, event: Event) -> bool:
        """Return True if the event is visible and not declined."""

        if any(
            attendee.is_self and attendee.response_status == ResponseStatus.DECLINED
            for attendee in event.attendees
        ):
            return False
        # Calendar enttiy may be limited to a specific event type
        if (
            self.entity_description.event_type is not None
            and self.entity_description.event_type != event.event_type
        ):
            return False
        # Default calendar entity omits the special types but includes all the others
        if (
            self.entity_description.event_type is None
            and event.event_type in FILTERED_EVENT_TYPES
        ):
            return False
        if self._ignore_availability:
            return True
        return event.transparency == OPAQUE

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()

        # We do not ask for an update with async_add_entities()
        # because it will update disabled entities. This is started as a
        # task to let if sync in the background without blocking startup
        self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self.coordinator.async_request_refresh(),
            "google.calendar-refresh",
        )

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Get all events in a specific time frame."""
        result_items = await self.coordinator.async_get_events(start_date, end_date)
        return [
            _get_calendar_event(
                event,
                self._event_colors,
                self.entity_description.initial_color,
                self.entity_description.foreground_color,
            )
            for event in filter(self._event_filter, result_items)
        ]

    def _event_with_offset(
        self,
    ) -> tuple[CalendarEvent | None, timedelta | None]:
        """Get the calendar event and offset if any."""
        if api_event := next(
            filter(
                self._event_filter,
                self.coordinator.upcoming or [],
            ),
            None,
        ):
            event = _get_calendar_event(
                api_event,
                self._event_colors,
                self.entity_description.initial_color,
                self.entity_description.foreground_color,
            )
            if self._offset:
                (event.summary, offset_value) = extract_offset(
                    event.summary, self._offset
                )
            return event, offset_value
        return None, None

    async def async_create_event(self, **kwargs: Any) -> None:
        """Add a new event to calendar."""
        dtstart = kwargs[EVENT_START]
        dtend = kwargs[EVENT_END]
        start: DateOrDatetime
        end: DateOrDatetime
        if isinstance(dtstart, datetime):
            start = DateOrDatetime(
                date_time=dt_util.as_local(dtstart),
                timezone=str(dt_util.get_default_time_zone()),
            )
            end = DateOrDatetime(
                date_time=dt_util.as_local(dtend),
                timezone=str(dt_util.get_default_time_zone()),
            )
        else:
            start = DateOrDatetime(date=dtstart)
            end = DateOrDatetime(date=dtend)
        event = Event.model_validate(
            {
                EVENT_SUMMARY: kwargs[EVENT_SUMMARY],
                "start": start,
                "end": end,
                EVENT_DESCRIPTION: kwargs.get(EVENT_DESCRIPTION),
            }
        )
        if location := kwargs.get(EVENT_LOCATION):
            event.location = location
        if attendees := kwargs.get(EVENT_ATTENDEES):
            event.attendees = _attendees_from_emails(attendees)
        if rrule := kwargs.get(EVENT_RRULE):
            event.recurrence = [f"{RRULE_PREFIX}{rrule}"]

        try:
            await cast(
                CalendarSyncUpdateCoordinator, self.coordinator
            ).sync.store_service.async_add_event(event)
        except ApiException as err:
            raise HomeAssistantError(f"Error while creating event: {err!s}") from err
        await self.coordinator.async_refresh()

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete an event on the calendar."""
        range_value: Range = Range.NONE
        if recurrence_range == Range.THIS_AND_FUTURE:
            range_value = Range.THIS_AND_FUTURE
        await cast(
            CalendarSyncUpdateCoordinator, self.coordinator
        ).sync.store_service.async_delete_event(
            ical_uuid=uid,
            event_id=recurrence_id,
            recurrence_range=range_value,
        )
        await self.coordinator.async_refresh()


def _get_calendar_event(
    event: Event,
    event_colors: Mapping[str, ColorDefinition],
    calendar_background_color: str | None,
    calendar_foreground_color: str | None,
) -> CalendarEvent:
    """Return a CalendarEvent from an API event."""
    rrule: str | None = None
    # Home Assistant expects a single RRULE: and all other rule types are unsupported or ignored
    if (
        len(event.recurrence) == 1
        and (raw_rule := event.recurrence[0])
        and raw_rule.startswith(RRULE_PREFIX)
    ):
        rrule = raw_rule.removeprefix(RRULE_PREFIX)
    color_id = getattr(event, "color_id", None)
    event_color = event_colors.get(color_id) if color_id else None
    background_color = (
        event_color.background if event_color else calendar_background_color
    )
    foreground_color = (
        event_color.foreground if event_color else calendar_foreground_color
    )
    return CalendarEvent(
        uid=event.ical_uuid,
        recurrence_id=event.id if event.recurring_event_id else None,
        rrule=rrule,
        summary=event.summary,
        start=event.start.value,
        end=event.end.value,
        description=event.description,
        location=event.location,
        color_id=color_id,
        background_color=background_color,
        foreground_color=foreground_color,
    )


async def async_create_event(entity: GoogleCalendarEntity, call: ServiceCall) -> None:
    """Add a new event to calendar."""
    start: DateOrDatetime | None = None
    end: DateOrDatetime | None = None
    hass = entity.hass

    if EVENT_IN in call.data:
        if EVENT_IN_DAYS in call.data[EVENT_IN]:
            now = datetime.now().date()

            start_in = now + timedelta(days=call.data[EVENT_IN][EVENT_IN_DAYS])
            end_in = start_in + timedelta(days=1)

            start = DateOrDatetime(date=start_in)
            end = DateOrDatetime(date=end_in)

        elif EVENT_IN_WEEKS in call.data[EVENT_IN]:
            now = datetime.now().date()

            start_in = now + timedelta(weeks=call.data[EVENT_IN][EVENT_IN_WEEKS])
            end_in = start_in + timedelta(days=1)

            start = DateOrDatetime(date=start_in)
            end = DateOrDatetime(date=end_in)

    elif EVENT_START_DATE in call.data and EVENT_END_DATE in call.data:
        start = DateOrDatetime(date=call.data[EVENT_START_DATE])
        end = DateOrDatetime(date=call.data[EVENT_END_DATE])

    elif EVENT_START_DATETIME in call.data and EVENT_END_DATETIME in call.data:
        start_dt = call.data[EVENT_START_DATETIME]
        end_dt = call.data[EVENT_END_DATETIME]
        start = DateOrDatetime(date_time=start_dt, timezone=str(hass.config.time_zone))
        end = DateOrDatetime(date_time=end_dt, timezone=str(hass.config.time_zone))

    if start is None or end is None:
        raise ValueError("Missing required fields to set start or end date/datetime")

    event = Event(
        summary=call.data[EVENT_SUMMARY],
        description=call.data[EVENT_DESCRIPTION],
        start=start,
        end=end,
    )
    if location := call.data.get(EVENT_LOCATION):
        event.location = location
    if attendees := call.data.get(EVENT_ATTENDEES):
        event.attendees = _attendees_from_emails(attendees)
    try:
        await cast(
            CalendarSyncUpdateCoordinator, entity.coordinator
        ).sync.api.async_create_event(
            entity.calendar_id,
            event,
        )
    except ApiException as err:
        raise HomeAssistantError(str(err)) from err
    entity.async_write_ha_state()
