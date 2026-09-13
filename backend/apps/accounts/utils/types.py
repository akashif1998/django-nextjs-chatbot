# /home/ram/aparsoft/backend/apps/accounts/utils/types.py

"""
Type definitions for the accounts app.
"""
from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class SocialAuthConnection:
    """Data structure for social auth connection."""
    provider_id: str
    profile_url: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: Optional[str] = None
    profile_data: Optional[Dict] = None
    connection_date: Optional[str] = None
    
    def __post_init__(self):
        """Validate and set defaults if needed."""
        if not self.profile_data:
            self.profile_data = {}
