"""

NOTE: For easy testing TwilML Bins are being used, therefore this flask server will not be required
    Instead stream.py includes a custom setup to handle the ws directly.


"""
from flask import Flask
from flask_cors import CORS
from flask_session import Session
from flask_sock import Sock

# from stream import handle_stream
from routes.voice import voice_bp

app = Flask(__name__)
CORS(app)
socket = Sock(app)

app.config['SECRET_KEY'] = 'based_phone_agent'
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

app.register_blueprint(voice_bp, url_prefix='/voice')

# @socket.route('/stream')
# async def _stream(ws):
#     await handle_stream(ws)

if __name__ == '__main__':
    app.run(debug=True)
