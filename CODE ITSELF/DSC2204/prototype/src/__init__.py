"""DSC2204 Timetabling prototype package."""
from .models import (
    Activity, ActivityType, Assignment, Calendar, Course, DeliveryMode,
    Group, Room, RoomType, TimeSlot, Timetable, Tutor, Universe,
)
from .data_loader import load_dsc_universe

__all__ = [
    "Activity", "ActivityType", "Assignment", "Calendar", "Course",
    "DeliveryMode", "Group", "Room", "RoomType", "TimeSlot", "Timetable",
    "Tutor", "Universe", "load_dsc_universe",
]
