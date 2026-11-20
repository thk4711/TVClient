/* Gemeinsame Grafik für HDMI-Ausgabe und prüfbare Vorschaubilder. */
#include "render.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "translations.h"
#include "qrcodegen.h"

static const char *translated(const struct artwork *a, const char *message) {
    if (a->german)
        for (unsigned i=0; i<sizeof(translations)/sizeof(*translations); i++)
            if (!strcmp(message, translations[i].en)) return translations[i].de;
    return message;
}

static const uint32_t white = 0xedf2f7, muted = 0xa4b0c0, blue = 0x67b6ff;
static const char *messages[] = {
    "Receiver starting …", "Loading settings …",
    "TV service starting …", "Loading channel …",
    "Waiting for network connection …", "Setup required",
    "Startup preparation failed", "Preparing setup …",
    "Imported settings need to be checked",
    "TV service not ready",
    "System startup is not progressing",
    "TV playback unavailable",
    "Error in satip-config.txt",
    "Data partition unavailable",
    "Initial setup not authorized",
    "Failed to prepare saved data",
    "TV initialization failed",
    "SSH/console setup failed",
    "Setup interface not ready",
    "No network address received",
    "TV playback is not starting",
    "Data preparation incomplete after 60 seconds",
    "Setup not prepared after 60 seconds",
    "Cannot read startup status",
    "Startup monitor has not responded for 60 seconds",
    "Device setup failed",
    "Setup interface starting …",
    "Connecting to SAT-IP server …",
    "SAT-IP server unreachable",
    "Preparing TV tuner …",
    "No active TV tuner ready",
    "Reloading channel …",
    "Connecting to Wi-Fi …",
    "Requesting network address …",
    "Wi-Fi not connected",
    "Starting TV player …",
    "Checking picture and sound …",
    "Ready", "Welcome"
};
static const int progress[] = {8, 24, 56, 86, 40, 100, 24, 32, 24, 56, 8, 86,
                              24, 24, 24, 24, 32, 32, 80, 48, 80, 24, 32, 24, 8, 32, 80,
                              64, 64, 72, 72, 86, 40, 48, 40, 80, 94, 100, 100};
static const char *hints[] = {
    "", "", "", "", "Check LAN cable or Wi-Fi connection", "Open setup in your browser",
    "Error code: /run/satip-boot-error · Console: Ctrl+Alt+F2",
    "", "Check import before watching TV; see documentation",
    "Diagnostics: /run/satip-supervisor/tvheadend.log",
    "Console: Ctrl+Alt+F2 · Check boot log over UART",
    "Diagnostics: /run/satip-supervisor/player.log · retrying …",
    "Check the file on the boot partition using a computer",
    "SD card: check satip-data partition, free space and write access",
    "Factory-empty image marker required; preserve existing data",
    "Error code: /run/satip-boot-error · Preserve existing data",
    "Error code: /run/satip-boot-error · Console: Ctrl+Alt+F2",
    "Check root_password / ssh_enabled · Read boot log over UART",
    "Diagnostics: /run/satip-supervisor/web.log",
    "Check LAN cable, router/DHCP and Wi-Fi entries in satip-config.txt",
    "Diagnostics: /run/satip-supervisor/player.log · Check HDMI configuration",
    "Last step: /run/satip-boot-stage · Read boot log over UART",
    "Last step: /run/satip-boot-stage · Read boot log over UART",
    "Diagnostics: /data/store/installation.json · Console: Ctrl+Alt+F2",
    "Diagnostics: /run/satip-splash-monitor.log · Console: Ctrl+Alt+F2",
    "Read boot log over UART · Check input devices/udev",
    "",
    "",
    "Turn on server; check network and server address · retrying …",
    "",
    "Tvheadend: check server registration and enabled DVB-S tuner",
    "Retrying connection; startup will continue",
    "",
    "Waiting for router/DHCP",
    "Turn on router; check Wi-Fi credentials and range · retrying …",
    "", "", "", ""
};
_Static_assert(sizeof(messages)/sizeof(*messages)==ARTWORK_STATE_COUNT, "state messages");
_Static_assert(sizeof(hints)/sizeof(*hints)==ARTWORK_STATE_COUNT, "state hints");
_Static_assert(sizeof(progress)/sizeof(*progress)==ARTWORK_STATE_COUNT, "state progress");

int artwork_waiting(int state) {
    return (state>=0 && state<=4) || state==7 || state==26 || state==27 || state==29 ||
           state==31 || state==32 || state==33 || state==35 || state==36;
}
int artwork_milestone(int state) {
    return progress[state>=0 && state<ARTWORK_STATE_COUNT ? state : 0];
}

static void config_hint(const struct artwork *a, const char *detail, char *output, size_t size) {
    char reason[32]; unsigned line; int used=0;
    const char *hint="Check the file on the boot partition using a computer";
    if (detail && sscanf(detail,"%31[a-z-]:%u%n",reason,&line,&used)==2 &&
        !detail[used] && line<=99999) {
        if (!strcmp(reason,"format")) hint="Write the entry as name=value";
        else if (!strcmp(reason,"unknown")) hint="Unknown field name; check spelling";
        else if (!strcmp(reason,"duplicate")) hint="Remove duplicate entry";
        else if (!strcmp(reason,"characters")) hint="Empty value or unsupported characters (ASCII)";
        else if (!strcmp(reason,"value")) hint="Unsupported value; see documentation for allowed values";
        else if (!strcmp(reason,"wifi")) hint="Specify wifi_country, wifi_ssid and wifi_psk together";
        else if (!strcmp(reason,"password-conflict")) hint="Use root_password; remove recovery_password_hash";
        else if (!strcmp(reason,"size")) hint="File too large (maximum 16 KiB)";
        else if (!strcmp(reason,"file")) hint="Cannot read file; save as UTF-8 text";
        if (line) { snprintf(output,size,translated(a,"Line %u: %s"),line,translated(a,hint)); return; }
    }
    snprintf(output,size,"%s",translated(a,hint));
}

static double scale(const struct artwork *a) {
    return fmin(a->width / 1920.0, a->height / 1080.0);
}
static int sx(const struct artwork *a, double x) {
    return (int)lround((a->width - 1920 * scale(a)) / 2 + x * scale(a));
}
static int sy(const struct artwork *a, double y) {
    return (int)lround((a->height - 1080 * scale(a)) / 2 + y * scale(a));
}
unsigned artwork_bar_top(unsigned width, unsigned height) {
    struct artwork a={.width=width,.height=height};
    return sy(&a,650);
}
unsigned artwork_bar_bottom(unsigned width, unsigned height) {
    struct artwork a={.width=width,.height=height};
    return sy(&a,654);
}
static uint32_t mix(uint32_t bg, uint32_t fg, unsigned alpha) {
    uint32_t result = 0;
    for (unsigned shift = 0; shift < 24; shift += 8)
        result |= ((((bg >> shift) & 255) * (255-alpha) +
                    ((fg >> shift) & 255) * alpha + 127) / 255) << shift;
    return result;
}
static void pixel(struct artwork *a, int x, int y, uint32_t color, unsigned alpha) {
    if (x >= 0 && y >= 0 && (unsigned)x < a->width && (unsigned)y < a->height) {
        uint32_t *p = a->pixels + y * a->stride + x;
        *p = mix(*p, color, alpha);
    }
}
static void rect(struct artwork *a, double x, double y, double w, double h, uint32_t color) {
    for (int j = sy(a,y); j < sy(a,y+h); j++)
        for (int i = sx(a,x); i < sx(a,x+w); i++) pixel(a,i,j,color,255);
}
/* Alle Texte stammen aus festen Meldungen oder einer begrenzten ASCII-IP-Adresse. */
static unsigned codepoint(const unsigned char **p) {
    unsigned c = *(*p)++;
    if (c < 128) return c;
    unsigned count = c < 224 ? 1 : c < 240 ? 2 : 3;
    c &= count == 1 ? 31 : count == 2 ? 15 : 7;
    while (count-- && **p) {
        if ((**p & 192)!=128) return 0xfffd;
        c = (c << 6) | (*(*p)++ & 63);
    }
    return c;
}
static void text(struct artwork *a, const char *value, double baseline, unsigned size,
                 uint32_t color, int bold) {
    value = translated(a, value);
    FT_Face face = bold ? a->bold : a->regular;
    if (!face) return;
    FT_Set_Pixel_Sizes(face, 0, (unsigned)fmax(1, lround(size * scale(a))));
    const unsigned char *p = (const unsigned char *)value;
    int width = 0;
    while (*p) {
        if (!FT_Load_Char(face, codepoint(&p), FT_LOAD_RENDER)) width += face->glyph->advance.x >> 6;
    }
    int x = ((int)a->width-width)/2, y = sy(a,baseline);
    p = (const unsigned char *)value;
    while (*p) {
        if (FT_Load_Char(face, codepoint(&p), FT_LOAD_RENDER)) continue;
        FT_GlyphSlot g = face->glyph;
        for (unsigned row=0; row<g->bitmap.rows; row++)
            for (unsigned col=0; col<g->bitmap.width; col++)
                pixel(a, x+g->bitmap_left+(int)col, y-g->bitmap_top+(int)row, color,
                      g->bitmap.buffer[row*g->bitmap.pitch+col]);
        x += g->advance.x >> 6;
    }
}
int artwork_open(struct artwork *a, unsigned width, unsigned height, const char *fonts) {
    memset(a, 0, sizeof(*a));
    a->progress=-1;
    if (!width || !height || width > 4096 || height > 2160) return -1;
    a->width=width; a->height=height; a->stride=width;
    a->pixels=calloc((size_t)width*height, sizeof(uint32_t));
    char path[1024];
    if (!a->pixels || FT_Init_FreeType(&a->library)) goto fail;
    snprintf(path,sizeof(path),"%s/DejaVuSans.ttf",fonts);
    if (FT_New_Face(a->library,path,0,&a->regular)) goto fail;
    snprintf(path,sizeof(path),"%s/DejaVuSans-Bold.ttf",fonts);
    if (FT_New_Face(a->library,path,0,&a->bold)) goto fail;
    return 0;
fail:
    artwork_close(a);
    return -1;
}
void artwork_close(struct artwork *a) {
    if (a->regular) FT_Done_Face(a->regular);
    if (a->bold) FT_Done_Face(a->bold);
    if (a->library) FT_Done_FreeType(a->library);
    free(a->pixels);
    memset(a,0,sizeof(*a));
}
/* Nine bounded UTF-8 lines and an optional URL. Only the root coordinator can
 * supply pages; passwords never leave its memory (only a mask is rendered). */
static void setup_page(struct artwork *a, const char *detail) {
    char page[2048];
    snprintf(page,sizeof(page),"%s",detail ? detail : "");
    char *lines[10]={0}, *cursor=page;
    for (unsigned i=0;i<10 && cursor;i++) {
        lines[i]=cursor; cursor=strchr(cursor,'\n');
        if (cursor) *cursor++=0;
    }
    text(a,"SAT-IP",140,58,white,1);
    for (unsigned i=0;i<9;i++) {
        if (!lines[i]) break;
        /* Fit long network names into the safe area without overrunning rows. */
        unsigned size=i==0 ? 40 : 30;
        if (strlen(lines[i])>70) size=24;
        text(a,lines[i],235+i*62,size,i==0 ? white : muted,i==0);
    }
    if (lines[9] && !strncmp(lines[9],"http://",7) && strlen(lines[9])<192) {
        uint8_t temp[qrcodegen_BUFFER_LEN_FOR_VERSION(10)], qr[qrcodegen_BUFFER_LEN_FOR_VERSION(10)];
        if (qrcodegen_encodeText(lines[9],temp,qr,qrcodegen_Ecc_MEDIUM,1,10,qrcodegen_Mask_AUTO,true)) {
            int n=qrcodegen_getSize(qr);
            int unit=(int)fmax(1,floor(330*scale(a)/(n+8)));
            int side=(n+8)*unit, left=((int)a->width-side)/2, top=sy(a,620);
            for (int y=0;y<side;y++) for (int x=0;x<side;x++) {
                int mx=x/unit-4,my=y/unit-4;
                bool dark=mx>=0 && my>=0 && mx<n && my<n && qrcodegen_getModule(qr,mx,my);
                pixel(a,left+x,top+y,dark ? 0 : 0xffffff,255);
            }
        }
    }
}
void artwork_draw(struct artwork *a, int state, const char *detail) {
    if (state < 0 || state >= ARTWORK_STATE_COUNT) state=0;
    /* Dezenter vertikaler Verlauf, auch in der mpv-Grafik identisch. */
    for (unsigned y=0; y<a->height; y++) {
        uint32_t color=mix(0x101318,0x080a0e, y*255/a->height);
        for (unsigned x=0; x<a->width; x++) a->pixels[y*a->stride+x]=color;
    }
    if (state==38) { setup_page(a,detail); return; }
    if (state==5 && detail && *detail) {
        char page[2048];
        snprintf(page,sizeof(page),"Welcome! Finish setup in your browser\nOn a phone or computer on the same home network, open:\n%s\nOr scan the QR code with your phone.\nNo password is needed.\n\n\n\n\n%s",detail,detail);
        setup_page(a,page); return;
    }
    /* Eigenständiges Empfangssymbol aus fünf abgerundeten Signalbalken. */
    const int heights[]={30,54,82,54,30};
    for (int i=0; i<5; i++) {
        double cx=912+i*24, cy=350, half=heights[i]/2.0;
        for (int y=sy(a,cy-half); y<=sy(a,cy+half); y++)
            for (int x=sx(a,cx-6); x<=sx(a,cx+6); x++) {
                double vx=(x-sx(a,cx))/scale(a), vy=fabs((y-sy(a,cy))/scale(a));
                double dy=fmax(0,vy-(half-6));
                unsigned alpha=(unsigned)(fmax(0,fmin(1,6.5-hypot(vx,dy)))*255);
                pixel(a,x,y,blue,alpha);
            }
    }
    text(a,"SAT-IP",502,96,white,1);
    text(a,"Television receiver",556,30,muted,0);
    text(a,messages[state],713,28,white,0);
    if (state==12) {
        char hint[256]; config_hint(a,detail,hint,sizeof(hint));
        text(a,hint,766,23,muted,0);
        text(a,"Edit satip-config.txt on the boot partition and restart",810,23,muted,0);
    } else if ((state==9 || state==18 || state==20) && detail && !strcmp(detail,"supervisor")) {
        const char *service=state==9 ? "tvheadend" : state==18 ? "web" : "player";
        char hint[128];
        snprintf(hint,sizeof(hint),translated(a,"Diagnostics: /run/satip-supervisor/%s.supervisor.log"),service);
        text(a,hint,766,23,muted,0);
    } else if (state==11) {
        const char *hint="Check player, HDMI/audio and storage";
        if (detail && !strcmp(detail,"load_failed")) hint="TV stream rejected; check Tvheadend access and tuner availability";
        else if (detail && !strcmp(detail,"load_timeout")) hint="No playable stream; check Tvheadend and SAT-IP reception";
        else if (detail && !strcmp(detail,"playback_stalled")) hint="Picture/audio stalled; check network and SAT-IP reception";
        else if (detail && !strcmp(detail,"audio_pid_missing")) hint="Saved audio track missing; check channel configuration";
        text(a,hint,766,23,muted,0);
        text(a,"Diagnostics: /run/satip-supervisor/player.log · retrying …",810,23,muted,0);
    } else if (state==5 && detail && *detail) text(a,detail,766,25,muted,0);
    else text(a,hints[state],766,23,muted,0);
}
void artwork_progress(struct artwork *a, void *pixels, unsigned stride, int state, unsigned tick) {
    if (state==38 || state==5) return;
    if (state < 0 || state >= ARTWORK_STATE_COUNT) state=0;
    uint32_t *saved=a->pixels; unsigned saved_stride=a->stride;
    a->pixels=pixels; a->stride=stride/4;
    rect(a,760,650,400,4,0x303946);
    int reached=a->progress<0 ? progress[state] : a->progress;
    if (reached>100) reached=100;
    rect(a,760,650,4*reached,4,!artwork_waiting(state) && state!=5 && state!=37 ? 0xf1c675 : blue);
    if (artwork_waiting(state)) {
        /* Bewegung innerhalb des erreichten Abschnitts, keine erfundene Prozentzahl. */
        int length=4*reached, x=(tick*5)%(length+32)-32;
        int start=x<0 ? 0 : x, end=x+32>length ? length : x+32;
        if (end>start) rect(a,760+start,650,end-start,4,white);
    }
    a->pixels=saved; a->stride=saved_stride;
}
