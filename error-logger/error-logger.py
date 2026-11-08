#!/usr/bin/env python3
"""Listens to the forbidden-requests Pub/Sub topic and logs each event to standard out."""

import json
import logging
from google.cloud import pubsub_v1

PROJECT_ID = "project-8aeecca0-4f70-4c6b-8e3"
SUBSCRIPTION_ID = "forbidden-requests-logger"

subscriber = pubsub_v1.SubscriberClient()
subscription_path = subscriber.subscription_path(PROJECT_ID, SUBSCRIPTION_ID)

def handle_message(message):
    event = json.loads(message.data.decode("utf-8"))
    print(f"Forbidden request: {event}")
    message.ack()


def main():
    # Opens a long-lived connection to Pub/Sub, starts a pool of background threads, and returns immediately. 
    # From then on, whenever a message arrives, one of those threads calls handle_message(message)
    streaming_pull = subscriber.subscribe(subscription_path, callback=handle_message)
    logging.info(f"Listening for messages on {subscription_path} (Ctrl+C to stop)")

    with subscriber:
        try:
            streaming_pull.result()  # blocks forever while messages arrive
        except KeyboardInterrupt:
            streaming_pull.cancel()
            streaming_pull.result()  # wait for the shutdown to finish
            logging.info("Shutting down.")


if __name__ == "__main__":
    main()