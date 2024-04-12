import asyncio
import base64
import json
import sys
from twilio.rest.conversations.v1 import conversation
import websockets
import ssl
from pydub import AudioSegment
import wave
import audioop
import numpy as np
import queue
import asyncio.locks
from openai import OpenAI
import os
from io import BytesIO

subscribers = {}

def is_chunk_silent(chunk, threshold=0.01, sample_width=2):
    audio_samples = np.frombuffer(chunk, dtype=np.int16 if sample_width == 2 else np.int8)
    rms_amplitude = np.sqrt(np.mean(audio_samples ** 2))
    normalized_rms = rms_amplitude / (2 ** (8 * sample_width - 1))
    return normalized_rms < threshold

def detect_interruption(chunk, threshold=0.8, sample_width=2):
    audio_samples = np.frombuffer(chunk, dtype=np.int16 if sample_width == 2 else np.int8)
    rms_amplitude = np.sqrt(np.mean(audio_samples ** 2))
    normalized_rms = rms_amplitude / (2 ** (8 * sample_width - 1))
    return normalized_rms > threshold

def deepgram_connect():
    deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")
    if not deepgram_api_key:
        raise Exception("DEEPGRAM_API_KEY environment variable not found.")

    extra_headers = {}
    extra_headers['Authorization'] = f'Token {deepgram_api_key}'
    deepgram_ws = websockets.connect('wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate=16000&channels=1&multichannel=false', extra_headers = extra_headers)

    return deepgram_ws

def encode_mpeg_to_payload(filename):
    audio = AudioSegment.from_file(filename, format="mp3")

    if audio.channels != 1 or audio.frame_rate != 8000:
        audio = audio.set_channels(1).set_frame_rate(8000)

    pcm_data = audio.raw_data

    ulaw_data = audioop.lin2ulaw(pcm_data, 2)

    base64_encoded_data = base64.b64encode(ulaw_data).decode('utf-8')

    return base64_encoded_data

async def interrupt_outbound(ws, callCtx):
    payload = {
        "event": "clear",
        "streamSid": callCtx["callSid"]
    }

    await ws.send(json.dumps(payload))

async def intro_message(ws, callCtx):
    try:
        encoded_audio = encode_mpeg_to_payload("intro.mpeg")

        payload = {
            "event": "media",
            "streamSid": callCtx["callSid"],
            "media": {
                "track": "outbound",
                "payload": encoded_audio
            }
        }

        await ws.send(json.dumps(payload))
    except Exception as e:
        print("intro_message error: ", e)

tools = [
  {
    "type": "function",
    "function": {
      "name": "end_call",
      "description": "End conversation with the user by closing line.",
      "parameters": {}
    }
  }
]

async def twilio_handler(twilio_ws):
    audio_queue = asyncio.Queue()
    callsid_queue = asyncio.Queue()
    outbound_queue = asyncio.Queue()
    trasncript_queue = asyncio.Queue()
    reply_queue = asyncio.Queue()

    # shared state
    # transcript_lock = asyncio.Lock()
    # transcript_buffer = ""
    # transcripts = []

    class SharedState:
        def __init__(self):
            self.transcript_buffer = ""
            self.transcripts = []
            self.is_responding = True

            self.conversation = [
                {
                    "role": "system",
                    "content": """
                        You are an assistant named Anakin, accessible on a call. Keep your conversation quick, simple and friendly. Here are the important things to keep in mind:
                        - DO NOT call the end_conversation function unless user asks you to end the conversation explicitely. Always ask if there is anyway you can help them further.
                        - Even when the user asks to end the call, CONFIRM AGAIN!
                        - Keep all the answers below 10 words! It is absolute necessary or user will drop.
                    """
                }
            ]

            self.lock = asyncio.Lock()

            openai_api_key = os.getenv("OPENAI_API_KEY")
            if not openai_api_key:
                raise Exception("OPENAI_API_KEY environment variable not found.")
            self.openai_client = OpenAI(api_key=openai_api_key)

    shared_state = SharedState()

    async with deepgram_connect() as deepgram_ws:

        async def deepgram_sender(deepgram_ws):
            print('deepgram_sender started \n')
            while True:
                chunk = await audio_queue.get()
                await deepgram_ws.send(chunk)

        async def deepgram_receiver(deepgram_ws, shared_state):
            try:
                # NOTE: only touched transcript_buffer string, and not transcripts array
                print('deepgram_receiver started \n')
                # we will wait until the twilio ws connection figures out the callsid
                # then we will initialize our subscribers list for this callsid
                callsid = await callsid_queue.get()
                subscribers[callsid] = []
                # for each deepgram result received, forward it on to all
                # queues subscribed to the twilio callsid
                async for message in deepgram_ws:
                    data = json.loads(message)  # Assuming message is a JSON string

                    # Ensure the message type is 'Results' and it's the final result for the channel
                    if data.get("type") == "Results" and data.get("is_final", False):
                        # Access the 'channel' data safely
                        channel_data = data.get("channel")
                        if channel_data:
                            # Access 'alternatives' safely
                            alternatives = channel_data.get("alternatives")
                            if alternatives:
                                # Assuming we're interested in the first alternative
                                transcript = alternatives[0].get("transcript")
                                if transcript:
                                    async with shared_state.lock:
                                        shared_state.transcript_buffer += transcript
                                # else:
                                #     print("No transcript available.")

                    for client in subscribers[callsid]:
                        client.put_nowait(message)

                # once the twilio call is over, tell all subscribed clients to close
                # and remove the subscriber list for this callsid
                for client in subscribers[callsid]:
                    client.put_nowait('close')

                del subscribers[callsid]
            except Exception as e:
                print("deepgram_receiver error: ", e)


        async def twilio_sender(twilio_ws):
            print("twilio_sender started \n")
            while True:
                payload = await outbound_queue.get()
                print(f"send event: {payload['event']}")
                if payload['event'] == 'stop':
                    await twilio_ws.close()
                await twilio_ws.send(json.dumps(payload))

        async def twilio_receiver(twilio_ws, shared_state):
            print('twilio_receiver started \n')
            # twilio sends audio data as 160 byte messages containing 20ms of audio each
            # we will buffer 20 twilio messages corresponding to 0.4 seconds of audio to improve throughput performance
            BUFFER_SIZE = 22 * 160

            inbuffer = bytearray(b'')
            outbuffer = bytearray(b'')
            inbound_chunks_started = False
            outbound_chunks_started = False
            latest_inbound_timestamp = 0
            latest_outbound_timestamp = 0

            # separately maintained buffer for tracking silence/interruptions
            audio_buffer = bytearray()

            CHUNK_DURATION_MS = 22
            SILENCE_DURATION_MS = 500
            SILENCE_DURATION_THRESHOLD = SILENCE_DURATION_MS / CHUNK_DURATION_MS # 1 seconds of silence
            SPEAKING_DURATION_THRESHOLD = 0.05 / (CHUNK_DURATION_MS / 1000.0)

            silent_chunk_count = 0
            speaking_chunk_count = 0
            interruption_freq_count = 0

            streamCtx = None

            async for message in twilio_ws:
                try:
                    data = json.loads(message)

                    if data["event"] == "stop":
                        print("call cancelled")
                        await twilio_ws.close()

                    if data['event'] == 'start':
                        start = data['start']
                        streamCtx = start
                        shared_state.streamCtx = start
                        print(start)

                        callsid = start['callSid']
                        callsid_queue.put_nowait(callsid)
                        print(callsid)

                        encoded_audio = encode_mpeg_to_payload("intro.mpeg")
                        # construct payload
                        payload = {
                            "event": "media",
                            "streamSid": streamCtx["streamSid"],
                            "media": {
                                "track": "outbound",
                                "payload": encoded_audio
                            }
                        }
                        # append to the outbound queue
                        outbound_queue.put_nowait(payload)

                    if data['event'] == 'connected':
                        continue

                    if data['event'] == 'media':
                        media = data['media']
                        chunk = base64.b64decode(media['payload'])

                        if media['track'] == 'inbound':
                            # fills in silence if there have been dropped packets
                            if inbound_chunks_started:
                                if latest_inbound_timestamp + 20 < int(media['timestamp']):
                                    bytes_to_fill = 8 * (int(media['timestamp']) - (latest_inbound_timestamp + 20))
                                    # NOTE: 0xff is silence for mulaw audio
                                    # and there are 8 bytes per ms of data for our format (8 bit, 8000 Hz)
                                    inbuffer.extend(b'\xff' * bytes_to_fill)
                            else:
                                # make it known that inbound chunks have started arriving
                                inbound_chunks_started = True
                                latest_inbound_timestamp = int(media['timestamp'])
                                # this basically sets the starting point for outbound timestamps
                                latest_outbound_timestamp = int(media['timestamp']) - 20

                            latest_inbound_timestamp = int(media['timestamp'])
                            # extend the inbound audio buffer with data
                            inbuffer.extend(chunk)

                            # check for silence/interruption
                            converted_audio_inbound = audioop.ulaw2lin(chunk, 2)
                            audio, _ = audioop.ratecv(converted_audio_inbound, 2, 1, 8000, 16000, None)

                            if is_chunk_silent(audio) and shared_state.is_responding is False:
                                silent_chunk_count += 1

                            if not is_chunk_silent(audio):
                                silent_chunk_count = 0
                                speaking_chunk_count += 1

                    if data['event'] == 'stop':
                        break

                    # check if our buffer is ready to send to our audio_queue (and, thus, then to deepgram)
                    while len(inbuffer) >= BUFFER_SIZE or len(outbuffer) >= BUFFER_SIZE:
                        converted_audio_inbound = audioop.ulaw2lin(inbuffer[:BUFFER_SIZE], 2)
                        audio, _ = audioop.ratecv(converted_audio_inbound, 2, 1, 8000, 16000, None)

                        asinbound = AudioSegment(audio, sample_width=2, frame_rate=16000, channels=1)
                        # not tracking outbound audio
                        # asoutbound = AudioSegment(outbuffer[:BUFFER_SIZE], sample_width=2, frame_rate=16000, channels=1)
                        mixed = AudioSegment.from_mono_audiosegments(asinbound)

                        # sending to deepgram via the audio_queue
                        audio_queue.put_nowait(mixed.raw_data)

                        # clearing buffers
                        inbuffer = inbuffer[BUFFER_SIZE:]
                        # outbuffer = outbuffer[BUFFER_SIZE:]

                        if silent_chunk_count > SILENCE_DURATION_THRESHOLD:
                            print("silence detected")
                            silent_chunk_count = 0
                            # check if transcript_buffer state needs to be saved
                            if not shared_state.is_responding:
                                async with shared_state.lock:
                                    shared_state.transcripts.append(shared_state.transcript_buffer)
                                    # print("Transcript: ", shared_state.transcript_buffer)
                                    trasncript_queue.put_nowait(shared_state.transcript_buffer)
                                    shared_state.transcript_buffer = ""

                        if speaking_chunk_count > SPEAKING_DURATION_THRESHOLD and shared_state.is_responding == True:
                            print("speech detected")
                            speaking_chunk_count = 0

                            # interrupt the response
                            payload = {
                                "event": "clear",
                                "streamSid": shared_state.streamCtx["streamSid"]
                            }

                            if interruption_freq_count >= 2:
                                interruption_freq_count = 0
                                # outbound_queue.put_nowait(payload)

                            interruption_freq_count += 1
                            shared_state.is_responding = False
                except Exception as e:
                    print("twilio_receiver error: ", e)
                    break

            # the async for loop will end if the ws connection from twilio dies
            # and if this happens, we should forward an empty byte to deepgram
            # to signal deepgram to send back remaining messages before closing
            audio_queue.put_nowait(b'')

        async def reply_handler(shared_state):
            print("started reply_handler \n")

            while True:
                transcript = await trasncript_queue.get()
                if transcript.strip():
                    print("User: ", transcript)
                    # start streaming response from LLM
                    shared_state.conversation.append({
                        "role": "user",
                        "content": transcript
                    })

                    response = shared_state.openai_client.chat.completions.create(
                        messages=shared_state.conversation,
                        model="gpt-3.5-turbo",
                        tools=tools
                    )

                    # check for function calling
                    if response.choices[0].message.tool_calls and response.choices[0].message.tool_calls[0].function.name == "end_call":
                        print("llm function call: end_call")
                        payload = {
                            "event": "stop",
                            "stop": {
                                "accountSid": shared_state.streamCtx["accountSid"],
                                "callSid": shared_state.streamCtx["callSid"]
                            },
                            "streamSid": shared_state.streamCtx["streamSid"]
                        }

                        outbound_queue.put_nowait(payload)
                        break

                    assistant_reply = response.choices[0].message.content
                    print(f"Assistant: {assistant_reply}")

                    reply_queue.put_nowait(assistant_reply)

                    shared_state.conversation.append({
                        "role": "assistant",
                        "content": assistant_reply
                    })

        async def tts_sender(shared_state):
            print("tts_sender started \n")
            while True:
                reply = await reply_queue.get()
                shared_state.is_responding = True

                response = shared_state.openai_client.audio.speech.create(
                    model="tts-1",
                    voice="alloy",
                    input=reply
                )

                batch_data = BytesIO()

                for index, chunk in enumerate(response.iter_bytes()):
                    if chunk:
                        audio_chunk = AudioSegment.from_file(BytesIO(chunk), format="mp3")
                        if audio_chunk.frame_rate != 8000 or audio_chunk.channels != 1:
                            audio_chunk = audio_chunk.set_frame_rate(8000).set_channels(1)

                        pcm_data = audio_chunk.raw_data

                        ulaw_data = audioop.lin2ulaw(pcm_data, audio_chunk.sample_width)

                        encoded_audio = base64.b64encode(ulaw_data).decode('utf-8')

                        payload = {
                            "event": "media",
                            "streamSid": shared_state.streamCtx["streamSid"],
                            "media": {
                                "track": "outbound",
                                "payload": encoded_audio
                            }
                        }

                        outbound_queue.put_nowait(payload)

                        if not shared_state.is_responding:
                            break

        await asyncio.wait([
            asyncio.ensure_future(deepgram_sender(deepgram_ws)),
            asyncio.ensure_future(deepgram_receiver(deepgram_ws, shared_state)),
            asyncio.ensure_future(twilio_receiver(twilio_ws, shared_state)),
            asyncio.ensure_future(twilio_sender(twilio_ws)),
            asyncio.ensure_future(reply_handler(shared_state)),
            asyncio.ensure_future(tts_sender(shared_state))
        ])

        await twilio_ws.close()

async def router(websocket, path):
    if path == '/stream':
        print('twilio connection incoming')
        await twilio_handler(websocket)

def main():
    server = websockets.serve(router, 'localhost', 8080)

    asyncio.get_event_loop().run_until_complete(server)
    asyncio.get_event_loop().run_forever()

if __name__ == '__main__':
    sys.exit(main() or 0)
