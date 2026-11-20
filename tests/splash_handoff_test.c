/* Linux/libdrm test harness: wraps hardware calls; never opens a DRM device. */
#define main splash_program_main
#include "../buildroot/external/package/satip-splash/src/splash.c"
#undef main
#include <assert.h>
#include <stdarg.h>

static unsigned closes, failed_close, modesets, removals, drops, tty_restores;
static bool own_frame=true, active_vt=true;

int __wrap_drmIoctl(int fd, unsigned long request, void *argument) {
    (void)fd; (void)argument;
    assert(request==DRM_IOCTL_MODE_CLOSEFB);
    closes++;
    if (closes==failed_close) { errno=ENOTTY; return -1; }
    return 0;
}
drmModeCrtcPtr __wrap_drmModeGetCrtc(int fd, uint32_t id) {
    (void)fd; (void)id;
    drmModeCrtcPtr crtc=calloc(1,sizeof(*crtc));
    crtc->mode_valid=1; crtc->buffer_id=own_frame ? 11 : 99;
    return crtc;
}
int __wrap_drmModeSetCrtc(int fd,uint32_t crtc,uint32_t fb,uint32_t x,uint32_t y,
                        uint32_t *connectors,int count,drmModeModeInfoPtr mode) {
    (void)fd; (void)crtc; (void)fb; (void)x; (void)y; (void)connectors; (void)count; (void)mode;
    modesets++; return 0;
}
int __wrap_drmModeRmFB(int fd,uint32_t id) { (void)fd; (void)id; removals++; return 0; }
int __wrap_drmDropMaster(int fd) { (void)fd; drops++; return 0; }
int __wrap_ioctl(int fd,unsigned long request,...) {
    (void)fd;
    va_list ap; va_start(ap,request);
    if (request==VT_GETSTATE) {
        struct vt_stat *vt=va_arg(ap,struct vt_stat *);
        vt->v_active=active_vt ? 1 : 2;
    } else { assert(request==KDSETMODE); tty_restores++; }
    va_end(ap); return 0;
}
static struct display fixture(void) {
    closes=modesets=removals=drops=tty_restores=failed_close=0;
    own_frame=active_vt=true;
    struct display d={.fd=open("/dev/null",O_RDWR),.tty=open("/dev/null",O_RDWR)};
    assert(d.fd>=0 && d.tty>=0);
    d.saved=calloc(1,sizeof(*d.saved));
    d.buffers[0].fb=11; d.buffers[1].fb=12;
    return d;
}
int main(void) {
    struct display d=fixture();
    assert(display_retain(&d));
    display_close(&d);
    assert(closes==2 && drops==1 && modesets==0 && removals==0 && tty_restores==0);
    assert(d.fd==-1 && d.tty==-1);

    d=fixture();
    display_close(&d);
    assert(closes==0 && drops==1 && modesets==1 && removals==2 && tty_restores==1);

    for (unsigned fail=1;fail<=2;fail++) {
        d=fixture(); failed_close=fail;
        assert(!display_retain(&d));
        display_close(&d);
        assert(drops==1 && modesets==1 && removals==3-fail && tty_restores==1);
    }
    d=fixture(); own_frame=false;
    assert(!display_retain(&d));
    display_close(&d);
    assert(closes==0 && modesets==1);

    d=fixture(); active_vt=false;
    assert(!display_retain(&d));
    display_close(&d);
    assert(closes==0 && tty_restores==1);
    puts("Handoff retention, restoring stop, unsupported ioctl, foreign framebuffer and VT checks passed");
    return 0;
}
