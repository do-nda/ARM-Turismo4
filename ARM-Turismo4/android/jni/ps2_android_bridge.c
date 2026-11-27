#ifdef __ANDROID__
#include <android/log.h>
#include <android/native_activity.h>
#include <android/native_window.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ps2rt.h"

#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  "GT4Recomp", __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "GT4Recomp", __VA_ARGS__)

static FILE *g_iso_fp = NULL;
static char g_core_path[512];
static char g_iso_path[512];

extern void ps2_gs_set_window(ANativeWindow *win);
extern int main(int argc, char **argv);

int ps2_cdvd_read_sectors(u32 lsn, u32 sectors, u8 *dst_ram) {
if (!g_iso_fp) return -1;
if (fseeko(g_iso_fp, (off_t)lsn * 2048LL, SEEK_SET) != 0) return -1;
size_t want = (size_t)sectors * 2048u;
return (fread(dst_ram, 1, want, g_iso_fp) == want) ? 0 : -1;
}

static void on_native_window_created(ANativeActivity *activity, ANativeWindow *window) {
(void)activity;
LOGI("ANativeWindow created (%dx%d)", ANativeWindow_getWidth(window), ANativeWindow_getHeight(window));
ps2_gs_set_window(window);
}

static void on_native_window_destroyed(ANativeActivity *activity, ANativeWindow *window) {
(void)activity; (void)window;
ps2_gs_set_window(NULL);
}

static void *guest_entry_thread(void *arg) {
(void)arg;
g_iso_fp = fopen(g_iso_path, "rb");
if (!g_iso_fp) {
    LOGE("Missing user-supplied PAL ISO at %s", g_iso_path);
    return NULL;
}
char *argv[] = { "gt4recomp", g_core_path, NULL };
main(2, argv);
return NULL;
}

void ANativeActivity_onCreate(ANativeActivity *activity, void *savedState, size_t savedStateSize) {
(void)savedState; (void)savedStateSize;
activity->callbacks->onNativeWindowCreated = on_native_window_created;
activity->callbacks->onNativeWindowDestroyed = on_native_window_destroyed;
snprintf(g_core_path, sizeof(g_core_path), "%s/core.elf", activity->externalDataPath);
snprintf(g_iso_path, sizeof(g_iso_path), "%s/GT4Europe.iso", activity->externalDataPath);
LOGI("GT4Recomp initialized. PAL ISO: %s | Core ELF: %s", g_iso_path, g_core_path);
pthread_t th;
pthread_create(&th, NULL, guest_entry_thread, NULL);
pthread_detach(th);
}
#endif
