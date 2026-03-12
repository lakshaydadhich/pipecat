
/**
 * Copyright (c) 2024–2025, Daily
 *
 * SPDX-License-Identifier: BSD 2-Clause License
 */

import {
  PipecatClient,
  PipecatClientOptions,
  RTVIEvent,
} from '@pipecat-ai/client-js';
import {
  ProtobufFrameSerializer,
  WebSocketTransport,
} from '@pipecat-ai/websocket-transport';

type TranscriptEvent = {
  text: string;
  final?: boolean;
};

type ServerMessage = {
  type?: string;
  text?: string;
  timestamp?: string;
};

class BlobCompatibleProtobufFrameSerializer extends ProtobufFrameSerializer {
  override async deserialize(data: unknown) {
    if (typeof Blob !== 'undefined') {
      if (data instanceof Uint8Array) {
        return super.deserialize(
          new Blob([
            data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength),
          ])
        );
      }
      if (data instanceof ArrayBuffer) {
        return super.deserialize(new Blob([new Uint8Array(data)]));
      }
    }
    return super.deserialize(data);
  }
}

class WebsocketClientApp {
  private pcClient: PipecatClient | null = null;
  private connectBtn: HTMLButtonElement | null = null;
  private disconnectBtn: HTMLButtonElement | null = null;
  private statusSpan: HTMLElement | null = null;
  private debugLog: HTMLElement | null = null;
  private botAudio: HTMLAudioElement;
  private userLiveLog: HTMLElement | null = null;
  private botLiveLog: HTMLElement | null = null;

  constructor() {
    console.log('WebsocketClientApp');

    const botAudioEl = document.getElementById('bot-audio') as
      | HTMLAudioElement
      | null;

    this.botAudio = botAudioEl ?? document.createElement('audio');
    this.botAudio.autoplay = true;

    this.setupDOMElements();
    this.setupEventListeners();
  }

  private setupDOMElements(): void {
    this.connectBtn = document.getElementById(
      'connect-btn'
    ) as HTMLButtonElement;

    this.disconnectBtn = document.getElementById(
      'disconnect-btn'
    ) as HTMLButtonElement;

    this.statusSpan = document.getElementById('connection-status');

    this.debugLog = document.getElementById('debug-log');
  }

  private setupEventListeners(): void {
    this.connectBtn?.addEventListener('click', () => this.connect());
    this.disconnectBtn?.addEventListener('click', () => this.disconnect());
  }

  private log(message: string, tsMs?: number): void {
    if (!this.debugLog) return;

    const entry = document.createElement('div');
    const when = tsMs ? new Date(tsMs) : new Date();
    entry.textContent = `${when.toISOString()} - ${message}`;

    if (message.startsWith('User: ')) {
      entry.style.color = '#021422';
    } else if (message.startsWith('Bot: ')) {
      entry.style.color = '#034405';
    }

    this.debugLog.appendChild(entry);
    this.debugLog.scrollTop = this.debugLog.scrollHeight;

    console.log(message);
  }

  private parseTimestampMs(raw?: string): number | undefined {
    if (!raw) return undefined;
    const ms = Date.parse(raw);
    return Number.isFinite(ms) ? ms : undefined;
  }

  private updateStatus(status: string): void {
    if (this.statusSpan) {
      this.statusSpan.textContent = status;
    }
    this.log(`Status: ${status}`);
  }

  private setLiveTranscriptInDebug(role: 'User' | 'Bot', text: string): void {
    if (!this.debugLog) return;

    const trimmed = text.trim();
    const target = role === 'User' ? 'userLiveLog' : 'botLiveLog';

    if (!this[target]) {
      const el = document.createElement('div');

      el.dataset.kind = 'live-transcript';
      el.style.opacity = '0.85';
      el.style.fontStyle = 'italic';
      el.style.marginBottom = '6px';

      this.debugLog.prepend(el);
      this[target] = el;
    }

    this[target]!.textContent = trimmed ? `[LIVE] ${role}: ${trimmed}` : '';
  }

  setupMediaTracks() {
    if (!this.pcClient) return;

    const tracks = this.pcClient.tracks();

    if (tracks.bot?.audio) {
      this.setupAudioTrack(tracks.bot.audio);
    }
  }

  setupTrackListeners() {
    if (!this.pcClient) return;

    this.pcClient.on(RTVIEvent.TrackStarted, (track, participant) => {
      if (!participant?.local && track.kind === 'audio') {
        this.setupAudioTrack(track);
      }
    });

    this.pcClient.on(RTVIEvent.TrackStopped, (track, participant) => {
      this.log(
        `Track stopped: ${track.kind} from ${participant?.name || 'unknown'}`
      );
    });
  }

  private setupAudioTrack(track: MediaStreamTrack): void {
    this.log('Setting up audio track');

    if (
      this.botAudio.srcObject &&
      'getAudioTracks' in this.botAudio.srcObject
    ) {
      const oldTrack = this.botAudio.srcObject.getAudioTracks()[0];
      if (oldTrack?.id === track.id) return;
    }

    this.botAudio.srcObject = new MediaStream([track]);
  }

  public async connect(): Promise<void> {
    try {
      const startTime = Date.now();

      const PipecatConfig: PipecatClientOptions = {
        transport: new WebSocketTransport({
          serializer: new BlobCompatibleProtobufFrameSerializer(),
        }),

        enableMic: true,
        enableCam: false,

        callbacks: {
          onConnected: () => {
            this.updateStatus('Connected');

            if (this.connectBtn) this.connectBtn.disabled = true;
            if (this.disconnectBtn) this.disconnectBtn.disabled = false;
          },

          onDisconnected: () => {
            this.updateStatus('Disconnected');

            if (this.connectBtn) this.connectBtn.disabled = false;
            if (this.disconnectBtn) this.disconnectBtn.disabled = true;

            this.log('Client disconnected');
          },

          onTransportStateChanged: (state) => {
            this.log(`Transport state: ${state}`);
          },

          onBotReady: (data) => {
            this.log(`Bot ready: ${JSON.stringify(data)}`);
            this.setupMediaTracks();
          },

          onUserTranscript: (data) => {
            const evt = data as TranscriptEvent;

            if (!evt.text) return;

            if (evt.final) {
              this.setLiveTranscriptInDebug('User', '');
              this.log(`User: ${evt.text}`);
            } else {
              this.setLiveTranscriptInDebug('User', evt.text);
            }
          },

          onBotTranscript: (data) => {
            const evt = data as TranscriptEvent;

            if (!evt.text) return;

            if (evt.final) {
              this.setLiveTranscriptInDebug('Bot', '');
              this.log(`Bot: ${evt.text}`);
            } else {
              this.setLiveTranscriptInDebug('Bot', evt.text);
            }
          },

          onServerMessage: (data) => {
            const msg = data as ServerMessage;
            const tsMs = this.parseTimestampMs(msg.timestamp);

            if (msg?.type === 'user_transcript_partial') {
              this.setLiveTranscriptInDebug('User', msg.text ?? '');
              return;
            }

            if (msg?.type === 'bot_transcript_partial') {
              this.setLiveTranscriptInDebug('Bot', msg.text ?? '');
              return;
            }

            if (msg?.type === 'user_transcript' || msg?.type === 'transcript_user') {
              this.setLiveTranscriptInDebug('User', '');
              this.log(`User: ${msg.text ?? ''}`, tsMs);
              return;
            }

            if (msg?.type === 'bot_transcript' || msg?.type === 'transcript_bot') {
              this.setLiveTranscriptInDebug('Bot', '');
              this.log(`Bot: ${msg.text ?? ''}`, tsMs);
              return;
            }

            if (msg?.text) {
              this.log(`Bot: ${msg.text}`, tsMs);
              return;
            }

            this.log(`Server: ${JSON.stringify(data)}`);
          },

          onMessageError: (message) =>
            this.log(`Message error: ${JSON.stringify(message)}`),

          onError: (message) => this.log(`Error: ${JSON.stringify(message)}`),
        },
      };

      this.pcClient = new PipecatClient(PipecatConfig);

      // @ts-ignore
      window.pcClient = this.pcClient;

      this.setupTrackListeners();

      this.log('Initializing devices...');
      await this.pcClient.initDevices();

      this.log('Connecting to bot...');

      await this.pcClient.startBotAndConnect({
        endpoint: '/connect',
      });

      const timeTaken = Date.now() - startTime;

      this.log(`Connection complete, timeTaken: ${timeTaken}`);
    } catch (error) {
      this.log(`Error connecting: ${(error as Error).message}`);
      this.updateStatus('Error');

      const pc = this.pcClient;
      this.pcClient = null;

      if (pc) {
        try {
          await pc.disconnect();
        } catch (disconnectError) {
          this.log(`Error during disconnect: ${disconnectError}`);
        }
      }
    }
  }

  public async disconnect(): Promise<void> {
    const pc = this.pcClient;

    this.pcClient = null;

    if (pc) {
      try {
        await pc.disconnect();

        if (
          this.botAudio.srcObject &&
          'getAudioTracks' in this.botAudio.srcObject
        ) {
          this.botAudio.srcObject
            .getAudioTracks()
            .forEach((track) => track.stop());

          this.botAudio.srcObject = null;
        }
      } catch (error) {
        this.log(`Error disconnecting: ${(error as Error).message}`);
      }
    }
  }
}

declare global {
  interface Window {
    WebsocketClientApp: typeof WebsocketClientApp;
  }
}

window.addEventListener('DOMContentLoaded', () => {
  window.WebsocketClientApp = WebsocketClientApp;
  new WebsocketClientApp();
});












