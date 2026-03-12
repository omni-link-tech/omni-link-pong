"""
WebSocket relay server for the Pong demo (with MQTT Context Publishing & Command Subscription).

This server listens on localhost:6789 and allows two types of clients to connect:

* The **game** client connects at path `/game`.
* The **agent** client connects at path `/agent`.

Features:
- Relays messages between Game and Agent.
- Parses Game State to track scores.
- Publishes Score Difference (Agent - AI) to MQTT topic 'olink/context' every 20s.
- Subscribes to MQTT topic 'olink/commands' to handle 'reset_game' and 'reset_score'.
"""

import asyncio
import json
import logging
from typing import Dict, Optional
import sys

import websockets

# --- MQTT Setup ---
try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("CRITICAL: paho-mqtt not installed. Please install it: pip install paho-mqtt")
    sys.exit(1)

MQTT_BROKER_HOST = "localhost"
MQTT_BROKER_PORT = 9001
MQTT_TOPIC_CONTEXT = "olink/context"
MQTT_TOPIC_COMMANDS = "olink/commands"
MQTT_TRANSPORT = "websockets"

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [WS-SERVER] - %(message)s')
logger = logging.getLogger("PongServer")

class PongRelayServer:
    """Relay between game and agent + MQTT Context Publisher & Command Subscriber."""

    def __init__(self, host: str = "localhost", port: int = 6789) -> None:
        self.host = host
        self.port = port
        self.clients: Dict[str, websockets.ServerConnection] = {}
        
        # State Tracking
        self.left_score = 0
        self.right_score = 0
        
        # Runtime Loop Reference (captured in run())
        self.loop = None
        
        # MQTT Client
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport=MQTT_TRANSPORT)
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        self.mqtt_client.on_message = self.on_mqtt_message

    def on_mqtt_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logger.info(f"✅ [MQTT CONNECTED] Link established to Broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
            client.subscribe(MQTT_TOPIC_COMMANDS)
            logger.info(f"✅ [MQTT SUBSCRIBED] Actively listening to topic: {MQTT_TOPIC_COMMANDS}")
            print(f"=================================================")
            print(f" OMNILINK MQTT COMMAND LISTENER ACTIVE & READY ")
            print(f" Topic: {MQTT_TOPIC_COMMANDS} ")
            print(f"=================================================")
        else:
            logger.error(f"❌ Failed to connect to MQTT, return code {reason_code}")

    def on_mqtt_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        logger.warning(f"Disconnected from MQTT (rc={reason_code})")

    def on_mqtt_message(self, client, userdata, msg):
        """Handle incoming MQTT commands."""
        print(f"\n🚨 [MQTT MESSAGE DETECTED] 🚨")
        print(f"Topic: {msg.topic}")
        try:
            raw_payload = msg.payload.decode()
            cleaned_payload = raw_payload.strip()
            
            print(f"Raw Payload: {raw_payload}")
            print(f"Cleaned Payload: {cleaned_payload}")
            logger.info(f"DEBUG: [MQTT] Raw received: '{raw_payload}', Cleaned: '{cleaned_payload}'")
            
            # Aggressive Substring Command Extraction (Bypasses JSON variance)
            lower_payload = cleaned_payload.lower()
            
            if "resume_game" in lower_payload:
                cleaned_payload = "resume_game"
            elif "pause_game" in lower_payload:
                cleaned_payload = "pause_game"
            elif "reset_score" in lower_payload:
                cleaned_payload = "reset_score"
            elif "reset_game" in lower_payload or '"reset"' in lower_payload or '"restart"' in lower_payload or "restart_game" in lower_payload:
                cleaned_payload = "reset_game"

            if cleaned_payload in ["reset_game", "reset_score", "pause_game", "resume_game"]:
                logger.info(f"DEBUG: [MQTT] Recognized command '{cleaned_payload}'. Converting to async task...")
                
                # Check Loop State
                if self.loop:
                    if self.loop.is_running():
                        logger.info("DEBUG: [Async] Loop is running. Scheduling 'send_admin_command'...")
                        future = asyncio.run_coroutine_threadsafe(self.send_admin_command(cleaned_payload), self.loop)
                        
                        # verify future completion (optional, adds complex non-blocking checks, skipping for now)
                    else:
                        logger.error("DEBUG: [Async] self.loop is NOT running!")
                else:
                    logger.error("DEBUG: [Async] self.loop is None!")
            else:
                logger.debug(f"DEBUG: [MQTT] Ignored unknown command: {cleaned_payload}")
                
        except Exception as e:
            logger.error(f"DEBUG: [MQTT] Error processing message: {e}")

    async def send_admin_command(self, command_str: str):
        """Sends the admin command to the connected game client."""
        logger.info(f"DEBUG: [Async] send_admin_command('{command_str}') called.")
        
        # Collect all possible sockets to fire the payload at just in case
        targets = []
        if "game" in self.clients:
            targets.append(self.clients["game"])
            
        if "agents" in self.clients:
            for ag in list(self.clients["agents"]):
                targets.append(ag)
                
        if targets:
            try:
                # Map payload to Game Protocol
                msg = {"type": "admin", "command": command_str}
                logger.info(f"DEBUG: [Async] Broadcasting to {len(targets)} Client(s): {msg}")
                for ws in targets:
                    await ws.send(json.dumps(msg))
                logger.info(f"DEBUG: [Async] Broadcasted '{command_str}' successfully.")
                
                # Reset local score tracking if needed
                if command_str == "reset_game" or command_str == "reset_score":
                    self.left_score = 0
                    self.right_score = 0
                    logger.info("DEBUG: [Async] Local scores reset.")
                    
            except websockets.ConnectionClosed:
                logger.warning("DEBUG: A client disconnected during command broadcast.")
            except Exception as e:
                 logger.error(f"DEBUG: [Async] Error sending broadcast: {e}")
        else:
            logger.warning(f"DEBUG: Received '{command_str}' but absolutely ZERO clients are connected.")

    async def start_mqtt(self):
        try:
            self.mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
            self.mqtt_client.loop_start() # Run in background thread
        except Exception as e:
            logger.error(f"Could not connect to MQTT Broker: {e}")

    async def mqtt_publisher_loop(self):
        """Publishes the latest game state to MQTT every 10 seconds."""
        logger.info(f"Starting MQTT Publisher (Topic: {MQTT_TOPIC_CONTEXT}, Interval: 10s)...")
        while True:
            await asyncio.sleep(10)
            try:
                if hasattr(self, 'latest_state') and self.latest_state:
                    self.mqtt_client.publish(MQTT_TOPIC_CONTEXT, self.latest_state)
                    logger.debug("DEBUG: [MQTT] Published 10-second contextual state snapshot.")
            except Exception as e:
                logger.error(f"MQTT Publish Error: {e}")

    async def mqtt_feedback_loop(self):
        """Publishes a heartbeat to the feedback topic every 60 seconds."""
        logger.info(f"Starting Feedback Publisher (Topic: olink/feedback, Interval: 60s)...")
        while True:
            await asyncio.sleep(60)
            try:
                feedback_data = {"feedback": "check status"}
                feedback_msg = json.dumps(feedback_data)
                self.mqtt_client.publish("olink/feedback", feedback_msg)
                logger.debug("DEBUG: [MQTT] Published 60-second feedback heartbeat.")
            except Exception as e:
                logger.error(f"MQTT Feedback Error: {e}")



    async def handler(self, websocket: websockets.ServerConnection) -> None:
        """Handle incoming WebSocket connections."""
        path = websocket.request.path
        role = None
        
        if path == "/game":
            role = "game"
        elif path == "/agent":
            role = "agent"
        else:
            await websocket.close(code=4000, reason="Unknown role")
            return

        # Register
        if role == "game":
            self.clients["game"] = websocket
        elif role == "agent":
            if "agents" not in self.clients:
                self.clients["agents"] = set()
            self.clients["agents"].add(websocket)
        
        logger.info(f"Client connected: {role}")

        try:
            async for message in websocket:
                # 1. State Parsing & Caching
                if role == "game":
                    # Cache the exact json string for the 10-second MQTT loop
                    self.latest_state = message
                        
                    try:
                        data = json.loads(message)
                        if data.get("type") == "state":
                            # Extract scores
                            scores = data.get("score", {})
                            self.left_score = scores.get("left", 0)
                            self.right_score = scores.get("right", 0)
                    except Exception:
                        pass # Don't break relay if parse fails

                # 2. Relay Logic
                if role == "game" and "agents" in self.clients:
                    for agent_ws in list(self.clients["agents"]):
                        try:
                            await agent_ws.send(message)
                        except websockets.ConnectionClosed:
                            pass
                elif role == "agent" and "game" in self.clients:
                    await self.clients["game"].send(message)
                    
                # Yield control to the event loop so threadsafe MQTT callbacks (admin commands) run instantly!
                await asyncio.sleep(0)
                    
        except websockets.ConnectionClosed:
            pass
        finally:
            if role == "game":
                if self.clients.get("game") is websocket:
                    del self.clients["game"]
            elif role == "agent" and "agents" in self.clients:
                self.clients["agents"].discard(websocket)
            logger.info(f"Client disconnected: {role}")

    async def run(self) -> None:
        # Capture current loop for threadsafe calls
        self.loop = asyncio.get_running_loop()
        
        # Start MQTT
        await self.start_mqtt()
        
        # Start Server and Publisher Concurrently
        async with websockets.serve(self.handler, self.host, self.port):
            logger.info(f"Server listening on {self.host}:{self.port}")
            await asyncio.gather(
                self.mqtt_publisher_loop(),
                self.mqtt_feedback_loop(),
                asyncio.Future()  # run forever
            )

if __name__ == "__main__":
    server = PongRelayServer()
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        logger.info("Server stopped.")