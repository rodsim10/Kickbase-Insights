"""
### This module holds all necessary functions to call Kickbase `/competitions/...` API endpoints.

TODO: Maybe list all functions here automatically?
"""

import requests
import logging
import json

from concurrent.futures import ThreadPoolExecutor

from backend import miscellaneous, exceptions

### -------------------------------------------------------------------

### How many team ids to probe at once
MAX_TEAM_WORKERS = 8

### Per-run caches
## main.py walks every player twice, in market_value_changes() and in taken_free_players()
## and pages the activity feed three times. None of that changes during a run, so each response is fetched once and reused
MAX_PLAYER_WORKERS = 8

_player_statistics_cache = {}
_player_marketvalue_cache = {}


def clear_caches() -> None:
    """### Empty the per-run API caches."""
    _player_statistics_cache.clear()
    _player_marketvalue_cache.clear()


def get_team_overview(token: str) -> dict:
    """### Get all team names + ID and their players.

    Args:
        token (str): The user's kkstrauth token.

    Returns:
        dict: A dictionary containing all team ids + names and players.
    """
    logging.info("Getting team overview...")

    url = "https://api.kickbase.com/v4/competitions/1/teams/{team_id}/teamprofile"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cookie": f"kkstrauth={token};",
    }

    ### There is no endpoint listing the teams of a competition, so the ids are probed (loop from ID 2 to 100)
    ## Team IDs 33 and 38 are skipped cuz they are leading to "500 Internal Server Error"
    team_ids = [team_id for team_id in range(2, 101) if team_id not in (33, 38)]

    def fetch_team(team_id):
        """Probe one team id. Returns the team info, or None if there is no such team."""
        try:
            response = requests.get(url.format(team_id=team_id), headers=headers)
            response.raise_for_status()  # Raise an HTTPError for bad responses (4xx and 5xx)
            if not response.content:  # Check if the response is not empty
                logging.warning(f"Empty response for team id {team_id}")
                return None
            
            json_response = response.json()
        except requests.exceptions.RequestException as e:
            logging.debug(f"Failed to get team id {team_id}: {e}")
            return None
        except json.JSONDecodeError as e:
            logging.warning(f"Failed to decode JSON for team id {team_id}: {e}")
            return None

        ### Check if team has players
        if not json_response["it"]:
            return None

        ### Get team id, name, and players
        return {
            "teamId": json_response["tid"],
            "teamName": json_response["tn"],
            "players": json_response["it"],
        }

    ### Most of these ids do not exist, and each probe is almost entirely spent waiting,
    ## so they run concurrently. 'map' keeps the results in team id order, which keeps
    ## STATIC_teams.json stable between runs.
    with ThreadPoolExecutor(max_workers=MAX_TEAM_WORKERS) as executor:
        results = list(executor.map(fetch_team, team_ids))

    all_teams = [team for team in results if team]

    logging.info("Got all teams.")

    ### Save to file
    miscellaneous.write_json_to_file(all_teams, "STATIC_teams.json")

    return all_teams


def match_days(token: str, competition_id: int = 1) -> tuple:
    """### Fetch all matches for every match day in the current season and save to JSON

    Args:
        token (str): The user's kkstrauth token
        competition_id (int): The competition ID (default: 1 which is the Bundesliga)
    
    Returns:
        tuple: A tuple containing the current match day number and a list of dictionaries. Each dictionary contains the match day number, the start date & time of the first match, and the start date & time of the last match.
    """
    url = f"https://api.kickbase.com/v4/competitions/{competition_id}/matchdays"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cookie": f"kkstrauth={token};",
    }

    match_days = []

    logging.info("Fetching match days...")

    try:
        response = requests.get(url, headers=headers).json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Request failed: {e}")

    current_match_day = response["day"]

    if response["it"]:
        for match_day in response["it"]:
            first_match = match_day["it"][0]["dt"] ### Start date & time of the first match
            last_match = match_day["it"][-1]["dt"] ### Start date & time of the last match

            match_days.append({
                "day": match_day["day"],
                "firstMatch": first_match,
                "lastMatch": last_match,
            })

    logging.info("Match days fetched.")

    ### Save to file
    miscellaneous.write_json_to_file(match_days, "match_days.json")

    ### TODO: Timestamp needed here?

    return current_match_day, match_days


def prefetch_players(token: str, league_id: str, player_ids) -> None:
    """### Fetch statistics and market value history for many players at once.

    market_value_changes() needs both for every player (stats + market value) in the competition.
    They run concurrently and fill the same caches the individual functions use.

    Args:
        token (str): The user's kkstrauth token.
        league_id (str): The league to fetch statistics for.
        player_ids (iterable): The player IDs to fetch.
    """
    ids = sorted({str(player_id) for player_id in player_ids})

    missing_statistics = [p for p in ids if (league_id, p) not in _player_statistics_cache]
    missing_marketvalues = [p for p in ids if p not in _player_marketvalue_cache]

    if not missing_statistics and not missing_marketvalues:
        return

    logging.debug(f"Prefetching {len(missing_statistics)} player statistic(s) "
                  f"and {len(missing_marketvalues)} market value history/histories...")

    with ThreadPoolExecutor(max_workers=MAX_PLAYER_WORKERS) as executor:
        futures = [executor.submit(player_statistics, token, league_id, p)
                   for p in missing_statistics]
        futures += [executor.submit(player_marketvalue, token, p)
                    for p in missing_marketvalues]

        ### Surface any exception rather than letting it disappear into the pool
        for future in futures:
            future.result()


def player_statistics(token: str, league_id: str, player_id: str):
    """
    ### Get the statistics of a given player.
    """
    cache_key = (league_id, str(player_id))
    if cache_key in _player_statistics_cache:
        return _player_statistics_cache[cache_key]

    url = f"https://api.kickbase.com/v4/competitions/1/players/{player_id}?leagueId={league_id}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": "de-DE,de;q=0.9", # localized for 'stxt' (status)
        "Cookie": f"kkstrauth={token};",
    }

    ### Send GET request to get the market value changes of ALL players in the league
    try:
        json_response = requests.get(url, headers=headers).json()
    except:
        raise exceptions.NotificatonException("Notification failed! Please check your Discord Webhook URL.") # TODO: Change exception

    _player_statistics_cache[cache_key] = json_response

    return json_response


def player_marketvalue(token: str, player_id: str):
    """
    ### Get the market value history of a given player.
    """
    cache_key = str(player_id)
    if cache_key in _player_marketvalue_cache:
        return _player_marketvalue_cache[cache_key]

    url_1year = f"https://api.kickbase.com/v4/competitions/1/players/{player_id}/marketValue/365"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cookie": f"kkstrauth={token};",
    }

    ### Send GET request to get the market value changes of ALL players in the league
    try:
        json_response = requests.get(url_1year, headers=headers).json()
    except:
        raise exceptions.NotificatonException("Notification failed! Please check your Discord Webhook URL.") # TODO: Change exception

    _player_marketvalue_cache[cache_key] = json_response["it"]

    return json_response["it"] ### Only return the "it" list

