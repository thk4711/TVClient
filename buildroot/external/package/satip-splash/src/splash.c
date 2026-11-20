#define _GNU_SOURCE
#include "render.h"
#include <errno.h>
#include <fcntl.h>
#include <linux/kd.h>
#include <linux/vt.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>

/* Linux 6.8+: close our FB reference without disabling its active plane.
 * Keep compatibility with older build-host libdrm headers; the ioctl is probed
 * at runtime and unsupported kernels use the normal restoring cleanup. */
#ifndef DRM_IOCTL_MODE_CLOSEFB
struct drm_mode_closefb { uint32_t fb_id, pad; };
#define DRM_IOCTL_MODE_CLOSEFB DRM_IOWR(0xD0, struct drm_mode_closefb)
#endif

#ifndef DIRECTORY
#define DIRECTORY "/run/satip-splash"
#endif
#define SOCKET DIRECTORY "/control"
#ifndef FONTS
#define FONTS "/usr/share/fonts/dejavu"
#endif
struct buffer { uint32_t handle, fb, pitch; uint64_t size; void *map; };
struct display {
    int fd, tty, old_kd;
    uint32_t connector, crtc;
    drmModeModeInfo mode;
    drmModeCrtc *saved;
    struct buffer buffers[2];
    struct artwork artwork;
    bool pending;
    double pending_since;
    unsigned front;
    bool keep_scanout;
};
static volatile sig_atomic_t stopped;
static void stop_signal(int sig) { (void)sig; stopped=1; }
static double now(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC,&ts);
    return ts.tv_sec+ts.tv_nsec/1e9;
}
static void display_close(struct display *d) {
    if (d->fd>=0) {
        if (d->saved) {
            if (!d->keep_scanout && d->saved->mode_valid)
                drmModeSetCrtc(d->fd,d->saved->crtc_id,d->saved->buffer_id,
                    d->saved->x,d->saved->y,&d->connector,1,&d->saved->mode);
            else if (!d->keep_scanout) drmModeSetCrtc(d->fd,d->crtc,0,0,0,NULL,0,NULL);
            drmModeFreeCrtc(d->saved);
        }
        for (unsigned i=0;i<2;i++) {
            struct buffer *b=&d->buffers[i];
            if (b->fb) drmModeRmFB(d->fd,b->fb);
            if (b->map && b->map!=MAP_FAILED) munmap(b->map,b->size);
            if (b->handle) {
                struct drm_mode_destroy_dumb destroy={.handle=b->handle};
                drmIoctl(d->fd,DRM_IOCTL_MODE_DESTROY_DUMB,&destroy);
            }
        }
        drmDropMaster(d->fd); close(d->fd);
    }
    if (d->tty>=0) {
        if (!d->keep_scanout) ioctl(d->tty,KDSETMODE,d->old_kd);
        close(d->tty);
    }
    artwork_close(&d->artwork);
    memset(d,0,sizeof(*d)); d->fd=d->tty=-1;
}
static bool display_retain(struct display *d) {
    if (d->fd<0 || d->tty<0) return false;
    struct vt_stat vt;
    if (ioctl(d->tty,VT_GETSTATE,&vt) || vt.v_active!=1) return false;
    drmModeCrtc *current=drmModeGetCrtc(d->fd,d->crtc);
    bool ours=current && current->mode_valid && current->buffer_id &&
        (current->buffer_id==d->buffers[0].fb || current->buffer_id==d->buffers[1].fb);
    if (current) drmModeFreeCrtc(current);
    if (!ours) return false;
    for (unsigned i=0;i<2;i++) {
        struct buffer *b=&d->buffers[i];
        if (!b->fb) continue;
        struct drm_mode_closefb closefb={.fb_id=b->fb};
        if (drmIoctl(d->fd,DRM_IOCTL_MODE_CLOSEFB,&closefb)) return false;
        b->fb=0; /* RMFB would disable the retained plane. */
    }
    d->keep_scanout=true;
    return true;
}
static int make_buffer(int fd, struct buffer *b, unsigned w, unsigned h) {
    struct drm_mode_create_dumb create={.width=w,.height=h,.bpp=32};
    if (drmIoctl(fd,DRM_IOCTL_MODE_CREATE_DUMB,&create)) return -1;
    b->handle=create.handle; b->pitch=create.pitch; b->size=create.size;
    uint32_t handles[4]={b->handle}, pitches[4]={b->pitch}, offsets[4]={0};
    if (drmModeAddFB2(fd,w,h,DRM_FORMAT_XRGB8888,handles,pitches,offsets,&b->fb,0)) return -1;
    struct drm_mode_map_dumb map={.handle=b->handle};
    if (drmIoctl(fd,DRM_IOCTL_MODE_MAP_DUMB,&map)) return -1;
    b->map=mmap(NULL,b->size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,map.offset);
    return b->map==MAP_FAILED ? -1 : 0;
}
static int choose_mode(drmModeConnector *c, unsigned w, unsigned h, unsigned hz) {
    int best=-1, score=-1;
    for (int i=0;i<c->count_modes;i++) {
        drmModeModeInfo *m=&c->modes[i];
        if (m->flags & DRM_MODE_FLAG_INTERLACE || m->hdisplay>1920 || m->vdisplay>1080) continue;
        int exact=m->hdisplay==w && m->vdisplay==h && abs((int)m->vrefresh-(int)hz)<=1;
        int rank=exact ? 10000000 : (m->type & DRM_MODE_TYPE_PREFERRED ? 3000000 : 0);
        rank+=m->hdisplay*m->vdisplay;
        if (rank>score) { best=i; score=rank; }
    }
    return best;
}
static int connector_open(struct display *d, int fd, drmModeRes *r,
                          drmModeConnector *c, unsigned w, unsigned h, unsigned hz) {
    int m=choose_mode(c,w,h,hz);
    if (m<0) return -1;
    uint32_t crtc=0;
    for (int i=0;i<c->count_encoders && !crtc;i++) {
        drmModeEncoder *e=drmModeGetEncoder(fd,c->encoders[i]);
        if (!e) continue;
        if (e->crtc_id) crtc=e->crtc_id;
        else for (int j=0;j<r->count_crtcs;j++)
            if (e->possible_crtcs & (1u<<j)) { crtc=r->crtcs[j]; break; }
        drmModeFreeEncoder(e);
    }
    if (!crtc || drmSetMaster(fd)) return -1;
    d->fd=fd; d->connector=c->connector_id; d->crtc=crtc; d->mode=c->modes[m];
    d->saved=drmModeGetCrtc(fd,crtc);
    d->tty=open("/dev/tty1",O_RDWR|O_CLOEXEC|O_NOCTTY);
    struct vt_stat vt;
    if (!d->saved || d->tty<0 || ioctl(d->tty,KDGETMODE,&d->old_kd) ||
        ioctl(d->tty,VT_GETSTATE,&vt) || vt.v_active!=1 || ioctl(d->tty,KDSETMODE,KD_GRAPHICS)) return -2;
    if (artwork_open(&d->artwork,d->mode.hdisplay,d->mode.vdisplay,FONTS)) return -2;
    for (int i=0;i<2;i++)
        if (make_buffer(fd,&d->buffers[i],d->mode.hdisplay,d->mode.vdisplay)) return -2;
    return 0;
}
static int display_open(struct display *d, unsigned port, unsigned w, unsigned h, unsigned hz) {
    /* Numerierung der DRM-Karten ist nicht stabil: ausschließlich VC4-KMS suchen. */
    for (int pass=0;pass<2;pass++) for (int card=0;card<16;card++) {
        char path[64]; snprintf(path,sizeof(path),"/dev/dri/card%d",card);
        int fd=open(path,O_RDWR|O_CLOEXEC|O_NONBLOCK);
        if (fd<0) continue;
        drmVersion *v=drmGetVersion(fd);
        bool vc4=v && v->name && !strcmp(v->name,"vc4");
        if (v) drmFreeVersion(v);
        if (!vc4) { close(fd); continue; }
        drmModeRes *r=drmModeGetResources(fd);
        if (!r) { close(fd); continue; }
        int result=-1;
        for (int i=0;i<r->count_connectors;i++) {
            drmModeConnector *c=drmModeGetConnector(fd,r->connectors[i]);
            if (!c) continue;
            bool preferred=c->connector_type_id==port;
            if (c->connector_type==DRM_MODE_CONNECTOR_HDMIA && c->connection==DRM_MODE_CONNECTED &&
                (pass==0 ? preferred : !preferred)) result=connector_open(d,fd,r,c,w,h,hz);
            drmModeFreeConnector(c);
            if (result!=-1) break;
        }
        drmModeFreeResources(r);
        if (result==0) {
            /* Root supervisor can hold a non-master client across last-close. */
            int device=open(DIRECTORY "/device.next",O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC|O_NOFOLLOW,0644);
            if (device>=0) {
                int written=dprintf(device,"%s\n",path); close(device);
                if (written>0) rename(DIRECTORY "/device.next",DIRECTORY "/device");
            }
            return 0;
        }
        if (result==-2) display_close(d); else close(fd);
    }
    return -1;
}
static void frame(struct display *d, unsigned index, int state, unsigned tick) {
    struct buffer *b=&d->buffers[index];
    for (unsigned y=0;y<d->artwork.height;y++)
        memcpy((char *)b->map+y*b->pitch,d->artwork.pixels+y*d->artwork.stride,d->artwork.width*4);
    artwork_progress(&d->artwork,b->map,b->pitch,state,tick);
}
static void flipped(int fd,unsigned seq,unsigned sec,unsigned usec,void *data) {
    (void)fd; (void)seq; (void)sec; (void)usec;
    struct display *d=data; d->pending=false; d->front=1-d->front;
}
static int server_open(void) {
    int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
    struct sockaddr_un address={.sun_family=AF_UNIX};
    strcpy(address.sun_path,SOCKET); unlink(SOCKET);
    if (fd<0 || bind(fd,(struct sockaddr *)&address,sizeof(address)) ||
        chmod(SOCKET,0600) || listen(fd,4)) {
        if (fd>=0) close(fd);
        return -1;
    }
    return fd;
}
static int export_frame(const char *width,const char *height,const char *state,const char *detail,const char *language,
                        const char *progress,const char *tick,bool bar) {
    if (strcmp(language,"en") && strcmp(language,"de")) return 1;
    char *end;
    unsigned long w=strtoul(width,&end,10); if (*end || w>4096) return 1;
    unsigned long h=strtoul(height,&end,10); if (*end || h>2160) return 1;
    unsigned long s=strtoul(state,&end,10); if (*end || s>=ARTWORK_STATE_COUNT) return 1;
    unsigned long p=progress ? strtoul(progress,&end,10) : (unsigned long)artwork_milestone(s);
    if ((progress && (!*progress || *end)) || p>100) return 1;
    unsigned long t=tick ? strtoul(tick,&end,10) : 24;
    if ((tick && (!*tick || *end)) || t>1000000) return 1;
    struct artwork a;
    if (artwork_open(&a,w,h,FONTS)) return 1;
    a.german=!strcmp(language,"de");
    a.progress=(int)p;
    unsigned top=bar ? artwork_bar_top(w,h) : 0;
    unsigned bottom=bar ? artwork_bar_bottom(w,h) : a.height;
    if (!bar) artwork_draw(&a,s,detail);
    artwork_progress(&a,a.pixels,a.stride*4,s,(unsigned)t);
    /* Bar frames are transparent outside the strip, covering the old bar only. */
    for (unsigned y=top;y<bottom;y++) for (unsigned x=0;x<a.width;x++)
        if (!bar || a.pixels[y*a.stride+x])
        a.pixels[y*a.stride+x] |= 0xff000000;
    size_t count=(size_t)a.width*(bottom-top);
    int result=fwrite(a.pixels+top*a.stride,4,count,stdout)!=count;
    if (fflush(stdout)) result=1;
    artwork_close(&a);
    return result;
}
int main(int argc,char **argv) {
    if ((argc==5 || argc==6 || argc==7 || argc==9) &&
        (!strcmp(argv[1],"--frame") || !strcmp(argv[1],"--bar")))
        return export_frame(argv[2],argv[3],argv[4],argc>=6 ? argv[5] : "", argc>=7 ? argv[6] : "en",
                            argc==9 ? argv[7] : NULL,argc==9 ? argv[8] : NULL,!strcmp(argv[1],"--bar"));
    if (argc!=1) return 1;
    umask(0077);
    if (geteuid()!=0 || (mkdir(DIRECTORY,0755) && errno!=EEXIST)) return 1;
    int lock=open(DIRECTORY "/lock",O_CREAT|O_RDWR|O_CLOEXEC|O_NOFOLLOW,0600);
    if (lock<0 || flock(lock,LOCK_EX|LOCK_NB)) return 1;
    if (!access(DIRECTORY "/finished",F_OK)) { close(lock); return 0; }
    chmod(DIRECTORY,0755);
    int server=server_open();
    if (server<0) { close(lock); return 1; }
    signal(SIGTERM,stop_signal); signal(SIGINT,stop_signal); signal(SIGPIPE,SIG_IGN);
    struct display d={.fd=-1,.tty=-1};
    unsigned port=2,w=1920,h=1080,hz=50,tick=0;
    int state=0,ack=-1,german=0; char detail[2048]="";
    bool dirty=true,handoff=false; double retry=0,began=now(),last_update=began,next_frame=0,check_output=0;
    double reached=8,advanced=began; int target=8;
    unsigned visible_logs=0;
    fprintf(stderr,"Startbildschirm bereit (%.3f s seit Systemstart)\n",began);
    while (!stopped) {
        struct pollfd fds[2]={{.fd=server,.events=POLLIN},{.fd=d.fd,.events=POLLIN}};
        if (poll(fds,2,100)<0 && errno!=EINTR) break;
        if (fds[0].revents & POLLIN) {
            int client=accept4(server,NULL,NULL,SOCK_CLOEXEC|SOCK_NONBLOCK);
            if (client>=0) {
                struct ucred peer; socklen_t len=sizeof(peer);
                struct pollfd p={.fd=client,.events=POLLIN}; char command[4160]={0};
                bool accepted=false;
                if (!getsockopt(client,SOL_SOCKET,SO_PEERCRED,&peer,&len) &&
                    peer.uid==0) {
                    size_t used=0; double deadline=now()+.1;
                    while (used<sizeof(command)-1 && now()<deadline && !strchr(command,'\n')) {
                        if (poll(&p,1,10)<=0) continue;
                        ssize_t n=recv(client,command+used,sizeof(command)-1-used,0);
                        if (n<=0) break;
                        used+=(size_t)n; command[used]=0;
                    }
                    if (strchr(command,'\n')) {
                        *strchr(command,'\n')=0;
                        if (!strcmp(command,"STOP") || !strcmp(command,"HANDOFF")) {
                            handoff=!strcmp(command,"HANDOFF"); stopped=1; ack=client; client=-1;
                        }
                        else if (peer.uid==0) {
                            unsigned a,b,c,e; int consumed=0;
                            if (!strncmp(command,"PAGE ",5)) {
                                size_t size=strlen(command+5);
                                char page[2048]={0}; bool valid=size%2==0 && size/2<sizeof(page);
                                for (size_t i=0;valid && i<size;i+=2) {
                                    char byte[3]={command[5+i],command[6+i],0}, *end;
                                    long value=strtol(byte,&end,16);
                                    if (*end || value==0 || (value<32 && value!=10)) valid=false;
                                    else page[i/2]=(char)value;
                                }
                                if (valid) {
                                    accepted=true; dirty=dirty || state!=38 || strcmp(detail,page);
                                    state=38; strcpy(detail,page); last_update=now();
                                }
                            } else if (!strcmp(command,"LANGUAGE en") || !strcmp(command,"LANGUAGE de")) {
                                int selected=!strcmp(command,"LANGUAGE de");
                                accepted=true; dirty=dirty || selected!=german; german=selected;
                            } else if (sscanf(command,"DISPLAY HDMI-A-%u %ux%u@%u%n",&a,&b,&c,&e,&consumed)==4 &&
                                !command[consumed] && (a==1 || a==2) &&
                                ((b==1920 && c==1080 && (e==50 || e==60)) || (b==1280 && c==720 && e==50))) {
                                accepted=true;
                                if (a!=port || b!=w || c!=h || e!=hz) {
                                    display_close(&d); port=a; w=b; h=c; hz=e; retry=0; dirty=true;
                                }
                            } else if (sscanf(command,"STATE %u%n",&a,&consumed)==1 && a<ARTWORK_STATE_COUNT &&
                                       (!command[consumed] || command[consumed]==' ')) {
                                const char *s=command+consumed; if (*s==' ') s++;
                                bool valid=strlen(s)<96;
                                for (const char *q=s;*q;q++) if (!strchr("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.:/-",*q)) valid=false;
                                if (valid) {
                                    accepted=true;
                                    if (state!=(int)a) {
                                        if (state==5) reached=target=artwork_milestone(a);
                                        else if (artwork_waiting(a) || a==5)
                                            target=target>artwork_milestone(a) ? target : artwork_milestone(a);
                                        else target=(int)reached;
                                        fprintf(stderr,"Startup display state %u, target %d%% (%.3f s since Linux start)\n",a,target,now());
                                    }
                                    dirty=dirty || state!=(int)a || strcmp(detail,s);
                                    state=(int)a; strcpy(detail,s); last_update=now();
                                }
                            }
                        }
                    }
                }
                if (client>=0) {
                    const char *reply=accepted ? "OK\n" : "FEHLER\n";
                    send(client,reply,strlen(reply),MSG_NOSIGNAL); close(client);
                }
            }
        }
        if (stopped || !access(DIRECTORY "/finished",F_OK)) break;
        if (now()-last_update>60 && (artwork_waiting(state) || state==5 || state==38)) {
            state=24; detail[0]=0; dirty=true;
            target=(int)reached;
        }
        reached=fmin(target,reached+40*(now()-advanced)); advanced=now();
        d.artwork.progress=(int)reached;
        if (d.fd<0 && now()>=retry) {
            if (!display_open(&d,port,w,h,hz)) {
                d.artwork.german=german;
                d.artwork.progress=(int)reached;
                artwork_draw(&d.artwork,state,detail); frame(&d,0,state,tick);
                if (drmModeSetCrtc(d.fd,d.crtc,d.buffers[0].fb,0,0,&d.connector,1,&d.mode)) display_close(&d);
                else if (visible_logs++<8) fprintf(stderr,"HDMI-Startbild sichtbar (%.3f s seit Systemstart)\n",now());
            }
            retry=now()+1; dirty=true;
        }
        if (d.fd<0) continue;
        if (now()>=check_output) {
            drmModeConnector *c=drmModeGetConnector(d.fd,d.connector);
            bool connected=c && c->connection==DRM_MODE_CONNECTED;
            if (c) drmModeFreeConnector(c);
            check_output=now()+1;
            if (!connected) { display_close(&d); continue; }
        }
        if (d.pending && now()-d.pending_since>2) { display_close(&d); continue; }
        struct vt_stat vt;
        if (!ioctl(d.tty,VT_GETSTATE,&vt) && vt.v_active!=1) break;
        if (fds[1].fd==d.fd && fds[1].revents & POLLIN) {
            drmEventContext context={.version=2,.page_flip_handler=flipped};
            if (drmHandleEvent(d.fd,&context)) { display_close(&d); continue; }
        }
        if (d.pending || now()<next_frame) continue;
        if (dirty) { d.artwork.german=german; artwork_draw(&d.artwork,state,detail); dirty=false; }
        frame(&d,1-d.front,state,tick++);
        next_frame=now()+.1;
        if (!drmModePageFlip(d.fd,d.crtc,d.buffers[1-d.front].fb,DRM_MODE_PAGE_FLIP_EVENT,&d)) {
            d.pending=true; d.pending_since=now();
        }
        else if (errno!=EBUSY) display_close(&d);
    }
    /* A supervisor marker can arrive before its socket command. Signals, VT
     * switches and failures still use the restoring cleanup. */
    handoff=handoff || (!stopped && !access(DIRECTORY "/finished",F_OK) &&
                       !access(DIRECTORY "/retain",F_OK));
    if (handoff) {
        bool retained=display_retain(&d);
        fprintf(stderr,"HDMI handoff: %s (%.3f s since Linux start)\n",
                retained ? "last frame retained" : "restoring display",now());
    }
    /* Lock/ack still cover closing all DRM/VT descriptors and releasing master.
     * The kernel owns the retained scanout until mpv replaces it. */
    int done=open(DIRECTORY "/finished",O_WRONLY|O_CREAT|O_CLOEXEC|O_NOFOLLOW,0644);
    if (done>=0) close(done);
    display_close(&d); unlink(SOCKET); close(server);
    if (ack>=0) { send(ack,"OK\n",3,MSG_NOSIGNAL); close(ack); }
    close(lock);
    return 0;
}
