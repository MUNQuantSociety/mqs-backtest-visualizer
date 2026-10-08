import os

import requests

from src.auth.jwt import create_discord_temp_token


# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------

DISCORD_REDIRECT_URI = os.environ["DISCORD_REDIRECT_URI"]


# ------------------------------------------------------------------
# DUMMY DATA
# TODO: Replace with DiscordRoleMapping database table
#
# Future idea:
#
# DiscordRoleMapping
# ------------------
# discord_role_id
# role_id
#
# The role_id would reference our application's Role table.
# ------------------------------------------------------------------

discord_role_mappings = {
    "1280966301019410442": {
        "discord_role_id": "1280966301019410442",
        "role_name": "admin",
    },
    "1280966637583077438": {
        "discord_role_id": "1280966637583077438",
        "role_name": "developer",
    },
}


# ------------------------------------------------------------------
# DISCORD CLIENT
# ------------------------------------------------------------------

class DiscordAPIError(Exception):
    pass


class DiscordClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        guild_id: str,
        timeout: int = 10,
    ):
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.guild_id = guild_id
        self.timeout = timeout

    def exchange_code(
        self,
        code: str,
        redirect_uri: str,
    ) -> dict:
        response = requests.post(
            f"{self.base_url}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            auth=(
                self.client_id,
                self.client_secret,
            ),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=self.timeout,
        )

        self._raise_for_discord_error(response)

        return response.json()

    def get_current_user(
        self,
        access_token: str,
    ) -> dict:
        response = requests.get(
            f"{self.base_url}/users/@me",
            headers={
                "Authorization": f"Bearer {access_token}",
            },
            timeout=self.timeout,
        )

        self._raise_for_discord_error(response)

        return response.json()

    def get_guild_member(
        self,
        access_token: str,
    ) -> dict:
        response = requests.get(
            f"{self.base_url}/users/@me/guilds/{self.guild_id}/member",
            headers={
                "Authorization": f"Bearer {access_token}",
            },
            timeout=self.timeout,
        )

        self._raise_for_discord_error(response)

        return response.json()

    def _raise_for_discord_error(
        self,
        response: requests.Response,
    ) -> None:
        if response.status_code < 400:
            return

        try:
            payload = response.json()
        except ValueError:
            payload = {
                "message": response.text,
            }

        raise DiscordAPIError(
            {
                "status_code": response.status_code,
                "discord_error": payload,
            }
        )


# ------------------------------------------------------------------
# DISCORD CLIENT INSTANCE
# ------------------------------------------------------------------

discord_client = DiscordClient(
    base_url=os.environ["DISCORD_BASE_URL"],
    client_id=os.environ["DISCORD_CLIENT_ID"],
    client_secret=os.environ["DISCORD_CLIENT_SECRET"],
    guild_id=os.environ["DISCORD_GUILD_ID"],
)


# ------------------------------------------------------------------
# DUMMY ROLE HELPERS
# ------------------------------------------------------------------

def get_mapped_role(
    discord_role_id: str,
) -> dict | None:
    return discord_role_mappings.get(
        discord_role_id
    )


# ------------------------------------------------------------------
# DISCORD REGISTRATION FLOW
# ------------------------------------------------------------------

def handle_discord_callback(code: str) -> str:
    token_response = discord_client.exchange_code(
        code,
        DISCORD_REDIRECT_URI,
    )

    discord_access_token = token_response["access_token"]

    discord_user = discord_client.get_current_user(
        discord_access_token
    )

    guild_member = discord_client.get_guild_member(
        discord_access_token
    )

    discord_roles = guild_member["roles"]

    print(discord_roles)

    mapped_roles: list[str] = []

    for discord_role_id in discord_roles:
        role_mapping = get_mapped_role(
            discord_role_id
        )

        if role_mapping is not None:
            mapped_roles.append(
                role_mapping["role_name"]
            )

    return create_discord_temp_token(
        discord_user_id=discord_user["id"],
        roles=mapped_roles,
    )
