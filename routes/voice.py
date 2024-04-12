import os
from flask import Blueprint, Response, request, session
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather, Stream, Start, Connect
import openai

"""
TODOs:
    - Handle interruptions
    - Reduce latency: try custom speed-to-text
"""

TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
WS_STREAM_URL = "wss://e4ef-69-80-152-94.ngrok-free.app/ws"

voice_bp = Blueprint('voice_bp', __name__)

@voice_bp.route("/incoming", methods=["POST"])
def handle_incoming():
    print("Received a call")
    resp = VoiceResponse()

    stream_url = f'wss://{request.host}/stream'
    print(stream_url)

    start = Connect()
    start.stream(url=stream_url)
    # stream = Stream(url=stream_url)
    resp.append(start)

    # gather = Gather(input='speech', action='/voice/process_speech', method='POST', timeout=5)
    # resp.say("hello! how can I help!", voice='women')
    # resp.pause(length=360)

    return Response(str(resp), mimetype='text/xml')

@voice_bp.route("/process_speech", methods=["POST"])
def process_speech():
    resp = VoiceResponse()

    session['message_count'] = session.get('message_count', 0) + 1

    speech_result = request.values.get("SpeechResult", None)

    if speech_result:
        resp.say(f"Message received. This was exchange number {session['message_count']}.", voice='alice')

        gather = Gather(input='speech', action='/voice/process_speech', method='POST', timeout=5)
        gather.say("How can I assist you further?", voice='alice')
        resp.append(gather)
    else:
        resp.say("I'm sorry, I didn't catch that. Could you please repeat?", voice='alice')

    return Response(str(resp), mimetype='text/xml')

@voice_bp.route("/fallback", methods=["POST"])
def fallback():
    resp = VoiceResponse()
    resp.say("I didn't receive any input. Goodbye!", voice='alice')
    resp.hangup()
    return Response(str(resp), mimetype='text/xml')
