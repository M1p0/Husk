/* SPDX-License-Identifier: GPL-2.0-or-later */
#define _DARWIN_C_SOURCE
#include "husk-tl-audio.h"

#include <AudioToolbox/AudioToolbox.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "husk-tl-bionic.h"
#include "husk-tl-cocos.h"

#define NBUF 3
#define FRAMES_PER_BUF 1024
#define RING_FRAMES 4096
#define OUT_RATE 48000
#define OUT_CHANNELS 2

/* Each engine output thread is a separate source. Concatenating their writes
 * into one ring makes two simultaneous streams each play at half speed. Keep
 * their clocks independent and mix them when the device asks for samples. */
typedef struct audio_source {
    struct audio_source *next;
    int16_t ring[RING_FRAMES * OUT_CHANNELS];
    size_t head, count;
    int rate, channels;
    uint64_t phase, written, played;
    int16_t prev[OUT_CHANNELS];
    bool have_prev, closed;
} audio_source;

static struct {
    pthread_mutex_t mu;
    pthread_cond_t cv;
    AudioQueueRef q;
    AudioQueueBufferRef bufs[NBUF];
    audio_source *sources;
    uint64_t rendered;
    bool paused, failed;
} A = { .mu = PTHREAD_MUTEX_INITIALIZER, .cv = PTHREAD_COND_INITIALIZER };

static pthread_key_t source_key;
static pthread_once_t source_once = PTHREAD_ONCE_INIT;

static void close_source(void *ptr)
{
    pthread_mutex_lock(&A.mu);
    ((audio_source *)ptr)->closed = true;
    pthread_mutex_unlock(&A.mu);
}

static void make_source_key(void) { pthread_key_create(&source_key, close_source); }

static audio_source *writer_source(void)
{
    pthread_once(&source_once, make_source_key);
    audio_source *s = pthread_getspecific(source_key);
    if (s) return s;
    s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    if (pthread_setspecific(source_key, s)) { free(s); return NULL; }
    pthread_mutex_lock(&A.mu);
    s->next = A.sources;
    A.sources = s;
    pthread_mutex_unlock(&A.mu);
    return s;
}

/* The audio thread asks for samples; an empty ring is silence, never a wait. */
static void on_buffer(void *user, AudioQueueRef q, AudioQueueBufferRef b)
{
    (void)user;
    int16_t *out = b->mAudioData;
    size_t want = FRAMES_PER_BUF;
    int64_t mix[FRAMES_PER_BUF * OUT_CHANNELS] = { 0 };
    pthread_mutex_lock(&A.mu);
    audio_source **link = &A.sources;
    while (*link) {
        audio_source *s = *link;
        size_t take = A.paused ? 0 : (s->count < want ? s->count : want);
        for (size_t i = 0; i < take; i++)
            for (size_t c = 0; c < OUT_CHANNELS; c++)
                mix[i * OUT_CHANNELS + c] += s->ring[((s->head + i) % RING_FRAMES) * OUT_CHANNELS + c];
        s->head = (s->head + take) % RING_FRAMES;
        s->count -= take;
        s->played += take;
        if (s->closed && !s->count) { *link = s->next; free(s); }
        else link = &s->next;
    }
    if (!A.paused) A.rendered += want;
    pthread_cond_broadcast(&A.cv);
    pthread_mutex_unlock(&A.mu);
    for (size_t i = 0; i < want * OUT_CHANNELS; i++)
        out[i] = (int16_t)(mix[i] > 32767 ? 32767 : mix[i] < -32768 ? -32768 : mix[i]);
    b->mAudioDataByteSize = (UInt32)(want * OUT_CHANNELS * sizeof(int16_t));
    AudioQueueEnqueueBuffer(q, b, 0, NULL);
}

static bool open_queue(void)
{
    AudioStreamBasicDescription f = { 0 };
    f.mSampleRate = OUT_RATE; f.mFormatID = kAudioFormatLinearPCM;
    f.mFormatFlags = kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked;
    f.mBitsPerChannel = 16; f.mChannelsPerFrame = OUT_CHANNELS; f.mFramesPerPacket = 1;
    f.mBytesPerFrame = f.mBytesPerPacket = OUT_CHANNELS * 2;
    OSStatus st = AudioQueueNewOutput(&f, on_buffer, NULL, NULL, NULL, 0, &A.q);
    if (st != noErr) { tl_log_line("audio: AudioQueueNewOutput failed (%d)", (int)st); return false; }
    for (int i = 0; i < NBUF; i++) {
        AudioQueueAllocateBuffer(A.q, FRAMES_PER_BUF * OUT_CHANNELS * 2, &A.bufs[i]);
        memset(A.bufs[i]->mAudioData, 0, FRAMES_PER_BUF * OUT_CHANNELS * 2);
        A.bufs[i]->mAudioDataByteSize = FRAMES_PER_BUF * OUT_CHANNELS * 2;
        AudioQueueEnqueueBuffer(A.q, A.bufs[i], 0, NULL);
    }
    st = AudioQueueStart(A.q, NULL);
    if (st != noErr) { tl_log_line("audio: AudioQueueStart failed (%d)", (int)st); return false; }
    tl_log_line("audio: output running, %d Hz, %d channel(s), independent source mixing", OUT_RATE, OUT_CHANNELS);
    return true;
}

static void sleep_for(int frames, int rate)
{
    struct timespec ts = { 0, (long)((double)frames * 1e9 / rate) };
    nanosleep(&ts, NULL);
}

/* A streaming resampler per source preserves pitch and duration even when
 * simultaneous writers use different rates. The phase survives write boundaries. */
static void host_write(const int16_t *samples, int frames, int channels, int rate)
{
    if (!samples || frames <= 0 || rate <= 0 || channels < 1 || channels > 2) return;
    audio_source *s = writer_source();
    if (!s) { sleep_for(frames, rate); return; }
    pthread_mutex_lock(&A.mu);
    if (!A.q && !A.failed) { if (!open_queue()) A.failed = true; }
    if (A.failed) { pthread_mutex_unlock(&A.mu); sleep_for(frames, rate); return; }
    if (s->rate != rate || s->channels != channels) {
        s->phase = 0; s->have_prev = false;
        s->rate = rate; s->channels = channels;
    }
    for (int f = 0; f < frames; f++) {
        int16_t cur[2] = { samples[(size_t)f * channels], samples[(size_t)f * channels + (channels == 1 ? 0 : 1)] };
        if (!s->have_prev) { memcpy(s->prev, cur, sizeof(cur)); s->have_prev = true; }
        while (s->phase < OUT_RATE) {
            while (s->count >= RING_FRAMES || A.paused) pthread_cond_wait(&A.cv, &A.mu);
            size_t tail = (s->head + s->count) % RING_FRAMES;
            for (int c = 0; c < OUT_CHANNELS; c++)
                s->ring[tail * OUT_CHANNELS + c] = (int16_t)(s->prev[c] +
                    ((int64_t)cur[c] - s->prev[c]) * (int64_t)s->phase / OUT_RATE);
            s->count++; s->written++;
            s->phase += (uint64_t)rate;
        }
        s->phase -= OUT_RATE;
        memcpy(s->prev, cur, sizeof(cur));
    }
    pthread_mutex_unlock(&A.mu);
}

/* TL_AUDIO_MUTE: pace like a device but make no sound (for test runs on a Mac). TL_AUDIO_STATS=1 also logs, about once a second,
 * how loud the game's output was, so a silent run still shows whether the game is making sound. */
static void muted_write(const int16_t *samples, int frames, int channels, int rate)
{
    static int stats = -1, seen, peak;
    static long loud, total;
    if (stats < 0) stats = getenv("TL_AUDIO_STATS") != NULL;
    if (stats) {
        for (int i = 0; i < frames * channels; i++) {
            int v = samples[i] < 0 ? -samples[i] : samples[i];
            if (v > peak) peak = v;
            if (v > 64) loud++;
        }
        total += frames * channels;
        if ((seen += frames) >= rate) {
            tl_log_line("audio: %d Hz x %d, peak %d, %.0f%% of samples audible", rate, channels, peak, 100.0 * loud / (total ? total : 1));
            seen = 0; peak = 0; loud = 0; total = 0;
        }
    }
    sleep_for(frames, rate);
}

void tl_audio_install(void) { tl_cocos_audio_hook = getenv("TL_AUDIO_MUTE") ? muted_write : host_write; }

void tl_audio_set_paused(bool paused)
{
    pthread_mutex_lock(&A.mu);
    A.paused = paused;
    if (A.q) { if (paused) AudioQueuePause(A.q); else AudioQueueStart(A.q, NULL); }
    pthread_cond_broadcast(&A.cv);
    pthread_mutex_unlock(&A.mu);
}
