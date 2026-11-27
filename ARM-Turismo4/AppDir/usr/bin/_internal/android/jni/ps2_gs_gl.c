#ifdef __ANDROID__
#include "ps2rt.h"
#include <android/log.h>
#include <android/native_window.h>
#include <EGL/egl.h>
#include <GLES3/gl3.h>
#include <pthread.h>
#include <string.h>

#define GS_LOGI(...) __android_log_print(ANDROID_LOG_INFO, "GT4Recomp", __VA_ARGS__)
#define GS_LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "GT4Recomp", __VA_ARGS__)

static pthread_mutex_t g_gs_mx = PTHREAD_MUTEX_INITIALIZER;
static ANativeWindow *g_win = NULL;
static EGLDisplay g_dpy = EGL_NO_DISPLAY;
static EGLSurface g_surf = EGL_NO_SURFACE;
static EGLContext g_ctx = EGL_NO_CONTEXT;
static GLuint g_prog = 0;
static GLuint g_vbo = 0;
static int g_win_w = 1280;
static int g_win_h = 720;

typedef struct {
float x, y, z;
float r, g, b, a;
} GsVertex;

#define MAX_GS_VERTS 65536
static GsVertex g_verts[MAX_GS_VERTS];
static int g_nverts = 0;

static u32 g_prim = 0;
static float g_cur_rgba[4] = {1.0f, 1.0f, 1.0f, 1.0f};
static GsVertex g_vqueue[4];
static int g_vq_len = 0;

static const char *VS_SRC =
"#version 300 es\n"
"layout(location=0) in vec3 aPos;\n"
"layout(location=1) in vec4 aCol;\n"
"out vec4 vCol;\n"
"void main() {\n"
"  gl_Position = vec4(aPos, 1.0);\n"
"  vCol = aCol;\n"
"}\n";

static const char *FS_SRC =
"#version 300 es\n"
"precision mediump float;\n"
"in vec4 vCol;\n"
"out vec4 oCol;\n"
"void main() { oCol = vCol; }\n";

static GLuint compile_shader(GLenum type, const char *src) {
GLuint s = glCreateShader(type);
glShaderSource(s, 1, &src, NULL);
glCompileShader(s);
return s;
}

static void init_egl_locked(void) {
if (!g_win || g_dpy != EGL_NO_DISPLAY) return;
g_dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
if (g_dpy == EGL_NO_DISPLAY || !eglInitialize(g_dpy, NULL, NULL)) {
    g_dpy = EGL_NO_DISPLAY;
    return;
}
const EGLint cfg_attr[] = {
    EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
    EGL_SURFACE_TYPE, EGL_WINDOW_BIT,
    EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
    EGL_DEPTH_SIZE, 16,
    EGL_NONE
};
EGLConfig cfg;
EGLint ncfg = 0;
if (!eglChooseConfig(g_dpy, cfg_attr, &cfg, 1, &ncfg) || ncfg < 1) return;
const EGLint ctx_attr[] = { EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE };
g_surf = eglCreateWindowSurface(g_dpy, cfg, g_win, NULL);
g_ctx = eglCreateContext(g_dpy, cfg, EGL_NO_CONTEXT, ctx_attr);
if (g_surf == EGL_NO_SURFACE || g_ctx == EGL_NO_CONTEXT) return;
eglMakeCurrent(g_dpy, g_surf, g_surf, g_ctx);
g_win_w = ANativeWindow_getWidth(g_win);
g_win_h = ANativeWindow_getHeight(g_win);
glViewport(0, 0, g_win_w, g_win_h);

GLuint vs = compile_shader(GL_VERTEX_SHADER, VS_SRC);
GLuint fs = compile_shader(GL_FRAGMENT_SHADER, FS_SRC);
g_prog = glCreateProgram();
glAttachShader(g_prog, vs);
glAttachShader(g_prog, fs);
glLinkProgram(g_prog);
glDeleteShader(vs);
glDeleteShader(fs);
glGenBuffers(1, &g_vbo);
glEnable(GL_BLEND);
glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
GS_LOGI("OpenGL ES 3.0 GS renderer ready (%dx%d)", g_win_w, g_win_h);
}

void ps2_gs_set_window(ANativeWindow *win) {
pthread_mutex_lock(&g_gs_mx);
if (g_dpy != EGL_NO_DISPLAY) {
    eglMakeCurrent(g_dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    if (g_surf != EGL_NO_SURFACE) eglDestroySurface(g_dpy, g_surf);
    if (g_ctx != EGL_NO_CONTEXT) eglDestroyContext(g_dpy, g_ctx);
    eglTerminate(g_dpy);
    g_dpy = EGL_NO_DISPLAY; g_surf = EGL_NO_SURFACE; g_ctx = EGL_NO_CONTEXT;
}
g_win = win;
pthread_mutex_unlock(&g_gs_mx);
}

static void push_tri(GsVertex a, GsVertex b, GsVertex c) {
if (g_nverts + 3 > MAX_GS_VERTS) return;
g_verts[g_nverts++] = a;
g_verts[g_nverts++] = b;
g_verts[g_nverts++] = c;
}

static void gs_kick_vertex(u32 xyz_lo, u32 xyz_hi, int draw) {
float x = ((float)(xyz_lo & 0xFFFFu) / 16.0f - 2048.0f) / 320.0f - 1.0f;
float y = 1.0f - ((float)((xyz_lo >> 16) & 0xFFFFu) / 16.0f - 2048.0f) / 256.0f;
float z = (float)xyz_hi / 4294967295.0f;
GsVertex v = { x, y, z, g_cur_rgba[0], g_cur_rgba[1], g_cur_rgba[2], g_cur_rgba[3] };
g_vqueue[g_vq_len & 3] = v;
g_vq_len++;
if (!draw) return;

u32 pt = g_prim & 7u;
if ((pt == 3u || pt == 4u || pt == 5u) && g_vq_len >= 3) {
    push_tri(g_vqueue[(g_vq_len - 3) & 3], g_vqueue[(g_vq_len - 2) & 3], v);
} else if (pt == 6u && g_vq_len >= 2) { /* 2D sprite rectangle */
    GsVertex a = g_vqueue[(g_vq_len - 2) & 3];
    GsVertex b = { v.x, a.y, v.z, v.r, v.g, v.b, v.a };
    GsVertex c = { a.x, v.y, v.z, v.r, v.g, v.b, v.a };
    push_tri(a, b, v);
    push_tri(a, v, c);
}
}

static void gs_write_reg(u8 reg, u64 val) {
if (reg == 0x00u) { /* PRIM */
    g_prim = (u32)(val & 0x7FFu);
    g_vq_len = 0;
} else if (reg == 0x01u) { /* RGBAQ */
    g_cur_rgba[0] = (float)(val & 0xFFu) / 255.0f;
    g_cur_rgba[1] = (float)((val >> 8) & 0xFFu) / 255.0f;
    g_cur_rgba[2] = (float)((val >> 16) & 0xFFu) / 255.0f;
    g_cur_rgba[3] = (float)((val >> 24) & 0xFFu) / 128.0f;
} else if (reg == 0x04u || reg == 0x05u || reg == 0x0Cu || reg == 0x0Du) {
    gs_kick_vertex((u32)val, (u32)(val >> 32), (reg == 0x04u || reg == 0x05u));
}
}

void ps2_gs_submit_gif(Ctx *ctx, u32 addr, u32 qwc) {
u32 p = addr & 0x1FFFFFFFu;
u32 end = p + qwc * 16u;
while (p + 16u <= end) {
    u64 tag_lo = ps2_rd64(ctx, p);
    u64 tag_hi = ps2_rd64(ctx, p + 8u);
    p += 16u;
    u32 nloop = (u32)(tag_lo & 0x7FFFu);
    u32 flg   = (u32)((tag_lo >> 58) & 3u);
    u32 nreg  = (u32)((tag_lo >> 60) & 0xFu);
    if (nreg == 0u) nreg = 16u;
    if (flg == 0u) { /* PACKED */
        for (u32 i = 0; i < nloop && p + 16u <= end; i++) {
            for (u32 r = 0; r < nreg && p + 16u <= end; r++) {
                u8 reg = (u8)((tag_hi >> (r * 4u)) & 0xFu);
                u64 d0 = ps2_rd64(ctx, p);
                u64 d1 = ps2_rd64(ctx, p + 8u);
                p += 16u;
                if (reg == 0x0Eu) gs_write_reg((u8)(d1 & 0xFFu), d0);
                else gs_write_reg(reg, d0);
            }
        }
    } else if (flg == 1u) { /* REGLIST */
        u32 total = nloop * nreg;
        for (u32 i = 0; i < total && p + 8u <= end; i++) {
            u8 reg = (u8)((tag_hi >> ((i % nreg) * 4u)) & 0xFu);
            u64 d = ps2_rd64(ctx, p);
            p += 8u;
            gs_write_reg(reg, d);
        }
        p = (p + 15u) & ~15u;
    } else { /* IMAGE */
        p += nloop * 16u;
    }
}
}

void ps2_gs_present_frame(void) {
pthread_mutex_lock(&g_gs_mx);
init_egl_locked();
if (g_dpy != EGL_NO_DISPLAY && g_surf != EGL_NO_SURFACE) {
    eglMakeCurrent(g_dpy, g_surf, g_surf, g_ctx);
    glClearColor(0.04f, 0.06f, 0.10f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
    if (g_nverts > 0 && g_prog) {
        glUseProgram(g_prog);
        glBindBuffer(GL_ARRAY_BUFFER, g_vbo);
        glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)(g_nverts * sizeof(GsVertex)), g_verts, GL_DYNAMIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, sizeof(GsVertex), (void *)0);
        glEnableVertexAttribArray(1);
        glVertexAttribPointer(1, 4, GL_FLOAT, GL_FALSE, sizeof(GsVertex), (void *)(3 * sizeof(float)));
        glDrawArrays(GL_TRIANGLES, 0, g_nverts);
    }
    eglSwapBuffers(g_dpy, g_surf);
}
g_nverts = 0;
pthread_mutex_unlock(&g_gs_mx);
}
#endif
