import logging

from os import getenv
from flask import Flask, jsonify
from flask_cors import CORS

from backend import exceptions
from backend.kickbase.v4 import competitions, user, leagues

import main

### ===============================================================================

### Get the needed environment variables
kb_mail = getenv("KB_MAIL")
kb_password = getenv("KB_PASSWORD")
discord_webhook = getenv("DISCORD_WEBHOOK")
preferred_league_name = getenv("KB_LIGA")

### ===============================================================================

app = Flask(__name__)
CORS(app)  # This will enable CORS for all routes

@app.route("/api/livepoints", methods=["GET"])
def get_live_points():
    logging.info("Flask API: Getting live points...")

    try:
        ### Login to Kickbase and pick the same league the periodic run uses
        user_info, user_token = user.login(kb_mail, kb_password, discord_webhook)

        league_list = leagues.get_league_list(user_token)
        if not league_list:
            return jsonify({"error": "No leagues found."}), 404

        selected_league = next((league for league in league_list if league.name == preferred_league_name), league_list[0])

        ### Long-running server: drop the cached live points so every refresh is fresh
        competitions.clear_caches()

        ### Reuse the periodic run's logic so the refresh output matches it exactly
        ## Relies on taken_players.json + league_user_stats.json from the last full run
        final_live_points = main.live_points(selected_league)
    except exceptions.KickbaseException as e:
        logging.error(f"Flask API: Failed to get live points: {e}")
        return jsonify({"error": str(e)}), 502

    logging.info("Flask API: Got live points.")

    ### Return the live points
    return jsonify(final_live_points)

if __name__ == "__main__":
    app.run()
