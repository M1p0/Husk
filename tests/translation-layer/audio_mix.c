/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Exercise the actual sink without opening the speakers. Only AudioQueue is
 * replaced: concurrent producers, resampling, backpressure and mixing are real. */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <unistd.h>
#include "../../src/translation-layer-next/husk-tl-audio.c"

void (*tl_cocos_audio_hook)(const int16_t *, int, int, int);
void tl_log_line(const char *fmt, ...) { (void)fmt; }

OSStatus AudioQueueNewOutput(const AudioStreamBasicDescription *f, AudioQueueOutputCallback cb,
                            void *user, CFRunLoopRef loop, CFStringRef mode, UInt32 flags, AudioQueueRef *q)
{
    (void)cb; (void)user; (void)loop; (void)mode; (void)flags;
    assert(f->mSampleRate == 48000 && f->mChannelsPerFrame == 2);
    *q = (AudioQueueRef)(uintptr_t)1;
    return noErr;
}
OSStatus AudioQueueAllocateBuffer(AudioQueueRef q, UInt32 bytes, AudioQueueBufferRef *b)
{
    (void)q;
    AudioQueueBuffer value = { .mAudioDataBytesCapacity = bytes, .mAudioData = calloc(1, bytes) };
    *b = malloc(sizeof(value)); memcpy(*b, &value, sizeof(value));
    return noErr;
}
OSStatus AudioQueueEnqueueBuffer(AudioQueueRef q, AudioQueueBufferRef b, UInt32 n, const AudioStreamPacketDescription *d)
{ (void)q; (void)b; (void)n; (void)d; return noErr; }
OSStatus AudioQueueStart(AudioQueueRef q, const AudioTimeStamp *t) { (void)q; (void)t; return noErr; }
OSStatus AudioQueuePause(AudioQueueRef q) { (void)q; return noErr; }

typedef struct { int rate, channels, value, frames; } producer;
static void *produce(void *arg)
{
    producer *p = arg;
    int16_t samples[257 * 2];
    for (int i = 0; i < 257 * 2; i++) samples[i] = (int16_t)p->value;
    for (int done = 0; done < p->frames;) {
        int n = p->frames - done < 257 ? p->frames - done : 257;
        tl_cocos_audio_hook(samples, n, p->channels, p->rate);
        done += n;
    }
    return NULL;
}

static void wait_buffered(int writers, size_t frames)
{
    for (int tries = 0; tries < 5000; tries++) {
        pthread_mutex_lock(&A.mu);
        int ready = 0;
        for (audio_source *s = A.sources; s; s = s->next) if (s->count >= frames) ready++;
        pthread_mutex_unlock(&A.mu);
        if (ready == writers) return;
        usleep(1000);
    }
    assert(!"audio producers failed to fill their independent buffers");
}

int main(void)
{
    tl_audio_install();
    producer p[] = { {48000, 2, 1000, 28800}, {44100, 2, 2000, 26460}, {24000, 1, 4000, 14400} };
    pthread_t threads[3];
    for (int i = 0; i < 3; i++) assert(pthread_create(&threads[i], NULL, produce, &p[i]) == 0);
    for (int done = 0; done < 28800;) {
        int n = 28800 - done < FRAMES_PER_BUF ? 28800 - done : FRAMES_PER_BUF;
        wait_buffered(3, (size_t)n);
        on_buffer(NULL, A.q, A.bufs[0]);
        int16_t *pcm = A.bufs[0]->mAudioData;
        for (int i = 0; i < FRAMES_PER_BUF * 2; i++) assert(pcm[i] == (i < n * 2 ? 7000 : 0));
        done += n;
    }
    for (int i = 0; i < 3; i++) pthread_join(threads[i], NULL);
    on_buffer(NULL, A.q, A.bufs[0]);
    assert(!A.sources); /* Exited writers release their buffers after draining. */
    puts("PASS: three concurrent rates play together in 0.6 seconds of device frames");

    int16_t ramp[300];
    for (int i = 0; i < 300; i++) ramp[i] = (int16_t)(i * 100);
    tl_cocos_audio_hook(ramp, 113, 1, 44100);
    tl_cocos_audio_hook(ramp + 113, 187, 1, 44100);
    audio_source *s = pthread_getspecific(source_key);
    assert(s->count == 327);
    tl_audio_set_paused(true);
    on_buffer(NULL, A.q, A.bufs[0]);
    assert(s->count == 327);
    for (int i = 0; i < FRAMES_PER_BUF * 2; i++) assert(((int16_t *)A.bufs[0]->mAudioData)[i] == 0);
    tl_audio_set_paused(false);
    on_buffer(NULL, A.q, A.bufs[0]);
    for (int i = 0; i < 327; i++) {
        double position = (double)i * 44100 / 48000 - 1;
        int expected = position <= 0 ? 0 : (int)(position * 100);
        int16_t *pcm = A.bufs[0]->mAudioData;
        assert(abs(pcm[i * 2] - expected) <= 1 && pcm[i * 2] == pcm[i * 2 + 1]);
    }
    puts("PASS: fractional resampling remains continuous across writes; pause preserves queued audio");
    return 0;
}
