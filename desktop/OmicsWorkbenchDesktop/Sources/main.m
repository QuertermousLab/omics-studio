// Omics Workbench: a native macOS window around Omics Studio's analysis pages (/workbench/ on the same server as
// Genome Canvas and Omics Canvas, http://171.65.68.140:8892): FASTQ download, bulk sequencing and single-cell
// pipelines, and the jobs they start. Pages are opened with ?desktop=1 so they leave the window chrome to this app.
// Modeled on Omics Canvas (desktop/OmicsCanvasDesktop), whose palette, fonts, GCFillView and GCButton are copied
// unchanged below. Added here: the jobs of the signed-in user are polled every 30 s (through the page, with its
// sign-in session), shown in the sidebar and the Dock badge, and announced with a notification when they end.
#import <AppKit/AppKit.h>
#import <CoreText/CoreText.h>
#import <WebKit/WebKit.h>
#import <UserNotifications/UserNotifications.h>
#import "OCConnection.h"
#include <math.h>

static NSString * const OWDefaultServerAddress = @"http://171.65.68.140:8892/";
static NSString * const OWAppearanceDefaultsKey = @"OmicsWorkbenchAppearance";
static NSString * const OWSectionDefaultsKey = @"OmicsWorkbenchSection";
/** {"<server> <user>": {job id: last state seen}}: which job states were already seen (and announced) */
static NSString * const OWJobStatesDefaultsKey = @"OmicsWorkbenchJobStates";
static NSString * const OWNotifyDefaultsKey = @"OmicsWorkbenchNotifyJobs";
static const NSTimeInterval OWPollInterval = 30;
static const NSUInteger OWSidebarJobs = 5;

#pragma mark - Nord palette

// These mirror the CSS custom properties in styles.css: :root is Nord
// "Snow Storm" (light) and html[data-theme="dark"] is Nord "Polar Night".
static NSColor *GCHex(uint32_t rgb, CGFloat alpha) {
    return [NSColor colorWithSRGBRed:((rgb >> 16) & 0xff) / 255.0 green:((rgb >> 8) & 0xff) / 255.0 blue:(rgb & 0xff) / 255.0 alpha:alpha];
}

static BOOL GCAppearanceIsDark(NSAppearance *appearance) {
    NSAppearanceName match = [appearance bestMatchFromAppearancesWithNames:@[NSAppearanceNameAqua, NSAppearanceNameDarkAqua]];
    return [match isEqualToString:NSAppearanceNameDarkAqua];
}

static NSColor *GCDynamic(uint32_t light, CGFloat lightAlpha, uint32_t dark, CGFloat darkAlpha) {
    NSColor *lightColor = GCHex(light, lightAlpha);
    NSColor *darkColor = GCHex(dark, darkAlpha);
    return [NSColor colorWithName:nil dynamicProvider:^NSColor *(NSAppearance *appearance) {
        return GCAppearanceIsDark(appearance) ? darkColor : lightColor;
    }];
}

#define GC_COLOR(name, light, lightAlpha, dark, darkAlpha) \
    static NSColor *name(void) { \
        static NSColor *color; static dispatch_once_t once; \
        dispatch_once(&once, ^{ color = GCDynamic(light, lightAlpha, dark, darkAlpha); }); \
        return color; \
    }

GC_COLOR(GCBackgroundColor, 0xe5e9f0, 1, 0x242933, 1)
GC_COLOR(GCRailColor, 0xe5e9f0, 1, 0x2a303b, 1)
GC_COLOR(GCSurfaceColor, 0xf8f9fb, 1, 0x2e3440, 1)
GC_COLOR(GCSurface3Color, 0xe5e9f0, 1, 0x3b4252, 1)
GC_COLOR(GCHoverColor, 0xe9edf2, 1, 0x394050, 1)
GC_COLOR(GCBorderColor, 0xd8dee9, 1, 0x3b4252, 1)
GC_COLOR(GCDividerColor, 0xe5e9f0, 1, 0x3b4252, 1)
GC_COLOR(GCTextColor, 0x2e3440, 1, 0xeceff4, 1)
GC_COLOR(GCText2Color, 0x434c5e, 1, 0xd8dee9, 1)
GC_COLOR(GCText3Color, 0x69728a, 1, 0x9aa5b8, 1)
GC_COLOR(GCText4Color, 0x9aa2b1, 1, 0x687286, 1)
GC_COLOR(GCAccentColor, 0x4c7a86, 1, 0x88c0d0, 1)
GC_COLOR(GCAccentHoverColor, 0x3f6873, 1, 0x9dcfdc, 1)
GC_COLOR(GCAccentSoftColor, 0xe0ebee, 1, 0x88c0d0, 0.14)
GC_COLOR(GCAccentTextColor, 0x3a6570, 1, 0x8fd3e3, 1)
GC_COLOR(GCOnAccentColor, 0xffffff, 1, 0x242933, 1)
GC_COLOR(GCBrandGlyphColor, 0x5e7f8c, 1, 0x5e81ac, 1)
GC_COLOR(GCSuccessColor, 0x7a9a5e, 1, 0xa3be8c, 1)
GC_COLOR(GCWarningColor, 0xd4a94f, 1, 0xebcb8b, 1)
GC_COLOR(GCDangerColor, 0xbf616a, 1, 0xbf616a, 1)

#pragma mark - Bundled fonts

// JetBrains Mono is the web interface's monospace font (SIL OFL; Manrope and Fraunces, the former text and heading faces, stay bundled),
// bundled as variable TrueType files in Resources/Fonts.
static void GCRegisterBundledFonts(void) {
    for (NSURL *url in [[NSBundle mainBundle] URLsForResourcesWithExtension:@"ttf" subdirectory:@"Fonts"] ?: @[]) {
        CTFontManagerRegisterFontsForURL((__bridge CFURLRef)url, kCTFontManagerScopeProcess, NULL);
    }
}

static NSFont *GCVariableFont(NSString *family, CGFloat size, CGFloat weight, BOOL monospaced) {
    static NSMutableDictionary<NSString *, NSFont *> *cache;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ cache = [NSMutableDictionary dictionary]; });
    NSString *key = [NSString stringWithFormat:@"%@-%.1f-%.0f", family, size, weight];
    NSFont *cached = cache[key];
    if (cached) return cached;
    NSFontDescriptor *descriptor = [NSFontDescriptor fontDescriptorWithFontAttributes:@{
        NSFontFamilyAttribute: family,
        NSFontVariationAttribute: @{@(0x77676874): @(weight)},  // 'wght'
    }];
    NSFont *font = [NSFont fontWithDescriptor:descriptor size:size];
    if (!font || ![font.familyName isEqualToString:family]) {
        NSFontWeight fallback = weight >= 600 ? NSFontWeightSemibold : weight >= 500 ? NSFontWeightMedium : NSFontWeightRegular;
        font = monospaced ? [NSFont monospacedSystemFontOfSize:size weight:fallback] : [NSFont systemFontOfSize:size weight:fallback];
    }
    cache[key] = font;
    return font;
}

// interface text in the system font (San Francisco), as the web pages (--font: system-ui; the user's choice, 2026-11-13)
static NSFont *GCUIFont(CGFloat size, CGFloat weight) {
    NSFontWeight w = weight >= 700 ? NSFontWeightBold : weight >= 600 ? NSFontWeightSemibold : weight >= 500 ? NSFontWeightMedium : NSFontWeightRegular;
    return [NSFont systemFontOfSize:size weight:w];
}
static NSFont *GCHeadingFont(CGFloat size) { return [NSFont systemFontOfSize:size weight:NSFontWeightSemibold]; }  // as the pages: the system font
static NSFont *GCMonoFont(CGFloat size, CGFloat weight) { return GCVariableFont(@"JetBrains Mono", size, weight, YES); }

static NSImage *GCSymbol(NSString *name, CGFloat pointSize, NSString *description) {
    NSImage *image = [NSImage imageWithSystemSymbolName:name accessibilityDescription:description];
    NSImageSymbolConfiguration *configuration = [NSImageSymbolConfiguration configurationWithPointSize:pointSize weight:NSFontWeightMedium];
    return [image imageWithSymbolConfiguration:configuration] ?: image;
}

#pragma mark - Themed views

// A rounded, filled and optionally stroked panel. It draws with dynamic colors
// in drawRect:, so it follows light/dark changes and renders in snapshots.
@interface GCFillView : NSView
@property(nonatomic, strong) NSColor *fillColor;
@property(nonatomic, strong) NSColor *strokeColor;
@property(nonatomic) CGFloat cornerRadius;
@property(nonatomic) BOOL clipsContent;
@end

@implementation GCFillView
- (instancetype)initWithFrame:(NSRect)frame {
    if ((self = [super initWithFrame:frame])) self.translatesAutoresizingMaskIntoConstraints = NO;
    return self;
}
- (void)setFillColor:(NSColor *)fillColor { _fillColor = fillColor; self.needsDisplay = YES; }
- (void)setStrokeColor:(NSColor *)strokeColor { _strokeColor = strokeColor; self.needsDisplay = YES; }
- (void)setCornerRadius:(CGFloat)cornerRadius {
    _cornerRadius = cornerRadius;
    if (self.layer) self.layer.cornerRadius = cornerRadius;
    self.needsDisplay = YES;
}
- (void)setClipsContent:(BOOL)clipsContent {
    _clipsContent = clipsContent;
    if (clipsContent) self.wantsLayer = YES;
    self.layer.cornerRadius = self.cornerRadius;
    self.layer.masksToBounds = clipsContent;
}
- (void)drawRect:(NSRect)dirtyRect {
    NSBezierPath *path = [NSBezierPath bezierPathWithRoundedRect:NSInsetRect(self.bounds, 0.5, 0.5) xRadius:self.cornerRadius yRadius:self.cornerRadius];
    if (self.fillColor) { [self.fillColor setFill]; [path fill]; }
    if (self.strokeColor) { [self.strokeColor setStroke]; path.lineWidth = 1; [path stroke]; }
}
@end

typedef NS_ENUM(NSInteger, GCButtonStyle) {
    GCButtonStylePrimary,    // .primary-button
    GCButtonStyleSecondary,  // .secondary-button / .source-actions button
    GCButtonStyleGhost,      // .quiet-button / .icon-button.ghost
    GCButtonStyleTool,       // .tool-button (toggles to the accent state)
    GCButtonStyleSegment,    // data-source segmented control
};

// Borderless NSButton with the web interface's button styles drawn behind
// AppKit's own image and title rendering.
@interface GCButton : NSButton
@property(nonatomic) GCButtonStyle style;
// AppKit flips `state` on every click, even for momentary push buttons, so the
// "on" appearance is drawn only for buttons that are meant to toggle.
@property(nonatomic) BOOL showsState;
@property(nonatomic, copy) NSString *label;
@property(nonatomic) BOOL hovered;
@property(nonatomic) CGFloat fixedHeight;
@property(nonatomic, strong) NSTrackingArea *hoverArea;
+ (instancetype)buttonWithLabel:(NSString *)label symbol:(NSString *)symbol style:(GCButtonStyle)style target:(id)target action:(SEL)action;
- (void)refreshColors;
@end

@implementation GCButton
+ (instancetype)buttonWithLabel:(NSString *)label symbol:(NSString *)symbol style:(GCButtonStyle)style target:(id)target action:(SEL)action {
    GCButton *button = [[self alloc] initWithFrame:NSZeroRect];
    button.translatesAutoresizingMaskIntoConstraints = NO;
    button.bordered = NO;
    button.style = style;
    button.target = target;
    button.action = action;
    button.fixedHeight = 32;
    button.font = GCUIFont(13, style == GCButtonStylePrimary ? 650 : 550);
    if (symbol.length) {
        button.image = GCSymbol(symbol, 13, label ?: symbol);
        button.imagePosition = label.length ? NSImageLeading : NSImageOnly;
        button.imageHugsTitle = YES;
    }
    button.label = label ?: @"";
    if (!label.length) button.toolTip = symbol;
    return button;
}
- (void)setLabel:(NSString *)label { _label = [label copy]; [self refreshColors]; [self invalidateIntrinsicContentSize]; }
- (void)setHovered:(BOOL)hovered { if (_hovered == hovered) return; _hovered = hovered; [self refreshColors]; }
- (void)setState:(NSControlStateValue)state { [super setState:state]; [self refreshColors]; }
- (void)setEnabled:(BOOL)enabled { [super setEnabled:enabled]; [self refreshColors]; }
- (void)setStyle:(GCButtonStyle)style { _style = style; [self refreshColors]; }
- (BOOL)isOn { return self.showsState && self.state == NSControlStateValueOn; }
- (NSColor *)foregroundColor {
    if (!self.enabled) return GCText4Color();
    BOOL on = [self isOn];
    switch (self.style) {
        case GCButtonStylePrimary: return GCOnAccentColor();
        case GCButtonStyleTool: return on ? GCAccentTextColor() : (self.hovered ? GCTextColor() : GCText2Color());
        case GCButtonStyleSegment: return on ? GCTextColor() : GCText3Color();
        default: return self.hovered ? GCTextColor() : GCText2Color();
    }
}
- (void)refreshColors {
    NSColor *foreground = [self foregroundColor];
    self.contentTintColor = foreground;
    NSMutableParagraphStyle *paragraph = [[NSMutableParagraphStyle alloc] init];
    paragraph.alignment = NSTextAlignmentCenter;
    paragraph.lineBreakMode = NSLineBreakByTruncatingTail;
    self.attributedTitle = [[NSAttributedString alloc] initWithString:self.label ?: @"" attributes:@{
        NSForegroundColorAttributeName: foreground,
        NSFontAttributeName: self.font ?: GCUIFont(13, 550),
        NSParagraphStyleAttributeName: paragraph,
    }];
    self.needsDisplay = YES;
}
- (NSSize)intrinsicContentSize {
    if (!self.label.length) return NSMakeSize(self.fixedHeight, self.fixedHeight);
    NSSize size = [super intrinsicContentSize];
    return NSMakeSize(ceil(size.width + 24), self.fixedHeight);
}
- (void)updateTrackingAreas {
    [super updateTrackingAreas];
    if (self.hoverArea) [self removeTrackingArea:self.hoverArea];
    self.hoverArea = [[NSTrackingArea alloc] initWithRect:NSZeroRect
        options:NSTrackingMouseEnteredAndExited | NSTrackingActiveInActiveApp | NSTrackingInVisibleRect owner:self userInfo:nil];
    [self addTrackingArea:self.hoverArea];
}
- (void)mouseEntered:(NSEvent *)event { self.hovered = YES; }
- (void)mouseExited:(NSEvent *)event { self.hovered = NO; }
- (void)viewDidChangeEffectiveAppearance { [super viewDidChangeEffectiveAppearance]; [self refreshColors]; }
- (void)drawRect:(NSRect)dirtyRect {
    BOOL active = self.enabled && (self.hovered || self.cell.isHighlighted);
    BOOL on = [self isOn];
    NSColor *fill = nil;
    NSColor *stroke = nil;
    CGFloat radius = self.style == GCButtonStyleSegment ? 7 : 9;
    switch (self.style) {
        case GCButtonStylePrimary:
            fill = !self.enabled ? GCSurface3Color() : (active ? GCAccentHoverColor() : GCAccentColor());
            break;
        case GCButtonStyleSecondary:
            fill = active ? GCHoverColor() : GCSurfaceColor();
            stroke = GCBorderColor();
            break;
        case GCButtonStyleTool:
            fill = on ? GCAccentSoftColor() : (active ? GCHoverColor() : GCSurfaceColor());
            stroke = on ? GCAccentColor() : GCBorderColor();
            break;
        case GCButtonStyleGhost:
            fill = active ? GCHoverColor() : nil;
            break;
        case GCButtonStyleSegment:
            fill = on ? GCSurfaceColor() : (active ? GCHoverColor() : nil);
            stroke = on ? GCBorderColor() : nil;
            break;
    }
    NSBezierPath *path = [NSBezierPath bezierPathWithRoundedRect:NSInsetRect(self.bounds, 0.5, 0.5) xRadius:radius yRadius:radius];
    if (fill) { [fill setFill]; [path fill]; }
    if (stroke) { [stroke setStroke]; path.lineWidth = 1; [path stroke]; }
    [super drawRect:dirtyRect];
}
@end

#pragma mark - Sections

// The pages this app shows. "page" is what the sidebar opens, "prefix" the URL paths that belong to it (the longest
// matching prefix wins, so every other /workbench/ page counts as the overview). Servers is for administrators only.
static NSArray<NSDictionary<NSString *, NSString *> *> *OWSections(void) {
    return @[
        @{@"key": @"overview", @"page": @"/workbench/", @"prefix": @"/workbench/", @"title": @"Overview", @"menu": @"Overview",
          @"detail": @"Recent jobs, server load", @"symbol": @"square.grid.2x2"},
        @{@"key": @"download", @"page": @"/workbench/download/", @"prefix": @"/workbench/download/", @"title": @"Download data", @"menu": @"Download Data",
          @"detail": @"FASTQ files", @"symbol": @"arrow.down.circle"},
        @{@"key": @"bulk", @"page": @"/workbench/bulk/", @"prefix": @"/workbench/bulk/", @"title": @"Bulk sequencing", @"menu": @"Bulk Sequencing",
          @"detail": @"RNA-seq, ChIP / CUT&RUN, ATAC", @"symbol": @"chart.bar.xaxis"},
        @{@"key": @"singlecell", @"page": @"/workbench/single-cell/", @"prefix": @"/workbench/single-cell/", @"title": @"Single-cell", @"menu": @"Single-cell",
          @"detail": @"Cell Ranger, Seurat", @"symbol": @"circle.hexagongrid"},
        @{@"key": @"jobs", @"page": @"/workbench/jobs/", @"prefix": @"/workbench/jobs/", @"title": @"Jobs", @"menu": @"Jobs",
          @"detail": @"Your analyses and their logs", @"symbol": @"list.bullet.rectangle"},
        @{@"key": @"servers", @"page": @"/admin/#servers", @"prefix": @"/admin/", @"title": @"Servers", @"menu": @"Servers",
          @"detail": @"Server load (administrators)", @"symbol": @"server.rack"},
    ];
}

static NSDictionary<NSString *, NSString *> *OWSectionForKey(NSString *key) {
    for (NSDictionary<NSString *, NSString *> *section in OWSections()) if ([section[@"key"] isEqualToString:key]) return section;
    return OWSections().firstObject;
}

static NSDictionary<NSString *, NSString *> *OWSectionForPath(NSString *path) {
    NSDictionary<NSString *, NSString *> *best = nil;
    for (NSDictionary<NSString *, NSString *> *section in OWSections()) {
        NSString *prefix = section[@"prefix"];
        BOOL match = [path hasPrefix:prefix] || [path isEqualToString:[prefix substringToIndex:prefix.length - 1]];
        if (match && prefix.length > [best[@"prefix"] length]) best = section;
    }
    return best;
}

static NSString *const OWGenomeCanvasBundleID = @"org.genomecanvas.desktop";

#pragma mark - Jobs (GET /workbench/api/jobs)

static NSString *OWString(id value) {
    if ([value isKindOfClass:[NSString class]]) return value;
    if ([value isKindOfClass:[NSNumber class]]) return [value stringValue];
    return nil;
}

static BOOL OWStateActive(NSString *state) { return [@[@"queued", @"starting", @"running", @"cancelling"] containsObject:state ?: @""]; }
static BOOL OWStateRunning(NSString *state) { return [@[@"starting", @"running"] containsObject:state ?: @""]; }
/** the ends that are announced (a cancelled job was stopped by its owner) */
static BOOL OWStateAnnounced(NSString *state) { return [@[@"done", @"failed"] containsObject:state ?: @""]; }

static NSColor *OWStateColor(NSString *state) {
    if (OWStateRunning(state)) return GCAccentColor();
    if ([state isEqualToString:@"queued"] || [state isEqualToString:@"cancelling"]) return GCWarningColor();
    if ([state isEqualToString:@"done"]) return GCSuccessColor();
    if ([state isEqualToString:@"failed"]) return GCDangerColor();
    return GCText4Color();
}

static NSString *OWKindLabel(NSString *kind) {
    if ([kind isEqualToString:@"download"]) return @"FASTQ download";
    if ([kind isEqualToString:@"bulk"]) return @"Bulk sequencing";
    if ([kind isEqualToString:@"singlecell"]) return @"Single-cell";
    return kind.length ? kind.capitalizedString : @"Analysis";
}

/** the server it runs on: its name ("server_name"), else its id */
static NSString *OWJobServer(NSDictionary *job) {
    NSString *name = OWString(job[@"server_name"]);
    return name.length ? name : OWString(job[@"server"]);
}

static NSString *OWJobTitle(NSDictionary *job) {
    NSString *title = OWString(job[@"title"]);
    return title.length ? title : [NSString stringWithFormat:@"Job %@", OWString(job[@"id"]) ?: @""];
}

/** "/workbench/jobs/<id>/": the job's log page */
static NSString *OWJobPath(NSString *jobId) {
    NSMutableCharacterSet *allowed = [[NSCharacterSet URLPathAllowedCharacterSet] mutableCopy];
    [allowed removeCharactersInString:@"/?#"];
    return [NSString stringWithFormat:@"/workbench/jobs/%@/", [jobId stringByAddingPercentEncodingWithAllowedCharacters:allowed] ?: @""];
}

/** seconds since 1970 (or milliseconds) or an ISO 8601 date; nil when it is neither */
static NSDate *OWDate(id value) {
    if ([value isKindOfClass:[NSNumber class]]) {
        double seconds = [value doubleValue];
        if (seconds > 1e12) seconds /= 1000;
        return seconds > 0 ? [NSDate dateWithTimeIntervalSince1970:seconds] : nil;
    }
    if (![value isKindOfClass:[NSString class]] || ![value length]) return nil;
    NSISO8601DateFormatter *formatter = [[NSISO8601DateFormatter alloc] init];
    NSDate *date = [formatter dateFromString:value];
    if (!date) {
        formatter.formatOptions = NSISO8601DateFormatWithInternetDateTime | NSISO8601DateFormatWithFractionalSeconds;
        date = [formatter dateFromString:value];
    }
    if (!date) {
        double seconds = [value doubleValue];
        if (seconds > 1e12) seconds /= 1000;
        if (seconds > 1e8) date = [NSDate dateWithTimeIntervalSince1970:seconds];
    }
    return date;
}

/** a job that ended while the app was not watching is announced only if it ended in the last day (or when unknown) */
static BOOL OWEndedRecently(NSDictionary *job) {
    NSDate *finished = OWDate(job[@"finished"]);
    return !finished || -[finished timeIntervalSinceNow] < 24 * 3600;
}

#pragma mark - Section card

// A page in the sidebar, drawn like Omics Canvas's viewer cards: icon tile, title, detail. The page showing gets
// the accent fill and border.
@interface OWSectionCard : NSButton
@property(nonatomic, copy) NSString *key;
@property(nonatomic, strong) GCFillView *tile;
@property(nonatomic, strong) NSImageView *glyph;
@property(nonatomic, strong) NSTextField *titleLabel;
@property(nonatomic, strong) NSTextField *detailLabel;
@property(nonatomic) BOOL hovered;
@property(nonatomic) BOOL chosen;
@property(nonatomic, strong) NSTrackingArea *hoverArea;
- (instancetype)initWithSection:(NSDictionary<NSString *, NSString *> *)section target:(id)target action:(SEL)action;
@end

@implementation OWSectionCard
- (instancetype)initWithSection:(NSDictionary<NSString *, NSString *> *)section target:(id)target action:(SEL)action {
    if (!(self = [super initWithFrame:NSZeroRect])) return nil;
    self.translatesAutoresizingMaskIntoConstraints = NO;
    self.bordered = NO;
    self.title = @"";
    self.key = section[@"key"];
    self.target = target;
    self.action = action;
    self.accessibilityLabel = section[@"title"];
    self.tile = [[GCFillView alloc] initWithFrame:NSZeroRect];
    self.tile.fillColor = GCBrandGlyphColor();
    self.tile.cornerRadius = 7;
    self.glyph = [NSImageView imageViewWithImage:GCSymbol(section[@"symbol"], 13, section[@"title"]) ?: [[NSImage alloc] init]];
    self.glyph.translatesAutoresizingMaskIntoConstraints = NO;
    self.glyph.contentTintColor = GCHex(0xeceff4, 1);
    [self.tile addSubview:self.glyph];
    self.titleLabel = [NSTextField labelWithString:section[@"title"]];
    self.titleLabel.font = GCUIFont(13, 650);
    self.titleLabel.textColor = GCTextColor();
    self.detailLabel = [NSTextField labelWithString:section[@"detail"]];
    self.detailLabel.font = GCUIFont(11.5, 450);
    self.detailLabel.textColor = GCText3Color();
    self.detailLabel.lineBreakMode = NSLineBreakByTruncatingTail;
    [self.detailLabel setContentCompressionResistancePriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSStackView *copy = [NSStackView stackViewWithViews:@[self.titleLabel, self.detailLabel]];
    copy.translatesAutoresizingMaskIntoConstraints = NO;
    copy.orientation = NSUserInterfaceLayoutOrientationVertical;
    copy.alignment = NSLayoutAttributeLeading;
    copy.spacing = 0;
    [self addSubview:self.tile];
    [self addSubview:copy];
    [NSLayoutConstraint activateConstraints:@[
        [self.tile.leadingAnchor constraintEqualToAnchor:self.leadingAnchor constant:8],
        [self.tile.centerYAnchor constraintEqualToAnchor:self.centerYAnchor],
        [self.tile.widthAnchor constraintEqualToConstant:26], [self.tile.heightAnchor constraintEqualToConstant:26],
        [self.glyph.centerXAnchor constraintEqualToAnchor:self.tile.centerXAnchor],
        [self.glyph.centerYAnchor constraintEqualToAnchor:self.tile.centerYAnchor],
        [copy.leadingAnchor constraintEqualToAnchor:self.tile.trailingAnchor constant:10],
        [copy.trailingAnchor constraintLessThanOrEqualToAnchor:self.trailingAnchor constant:-10],
        [copy.centerYAnchor constraintEqualToAnchor:self.centerYAnchor],
        [self.heightAnchor constraintEqualToConstant:42],
    ]];
    return self;
}
- (NSView *)hitTest:(NSPoint)point { return [super hitTest:point] ? self : nil; }
- (void)setHovered:(BOOL)hovered { _hovered = hovered; self.needsDisplay = YES; }
- (void)setChosen:(BOOL)chosen {
    _chosen = chosen;
    self.tile.fillColor = chosen ? GCAccentColor() : GCBrandGlyphColor();
    self.titleLabel.textColor = chosen ? GCAccentTextColor() : GCTextColor();
    self.needsDisplay = YES;
}
- (void)updateTrackingAreas {
    [super updateTrackingAreas];
    if (self.hoverArea) [self removeTrackingArea:self.hoverArea];
    self.hoverArea = [[NSTrackingArea alloc] initWithRect:NSZeroRect
        options:NSTrackingMouseEnteredAndExited | NSTrackingActiveInActiveApp | NSTrackingInVisibleRect owner:self userInfo:nil];
    [self addTrackingArea:self.hoverArea];
}
- (void)mouseEntered:(NSEvent *)event { self.hovered = YES; }
- (void)mouseExited:(NSEvent *)event { self.hovered = NO; }
- (void)viewDidChangeEffectiveAppearance { [super viewDidChangeEffectiveAppearance]; self.chosen = self.chosen; }
- (void)drawRect:(NSRect)dirtyRect {
    NSRect frame = NSInsetRect(self.bounds, 1, 1);
    NSBezierPath *path = [NSBezierPath bezierPathWithRoundedRect:NSInsetRect(frame, 0.5, 0.5) xRadius:10 yRadius:10];
    [(self.chosen ? GCAccentSoftColor() : (self.hovered ? GCHoverColor() : GCSurfaceColor())) setFill];
    [path fill];
    [(self.chosen || self.hovered ? GCAccentColor() : GCBorderColor()) setStroke];
    path.lineWidth = 1;
    [path stroke];
}
@end

#pragma mark - Job rows

// A job in the sidebar: state dot, title, "Running · <step>". Click: its log page.
@interface OWJobRow : NSButton
@property(nonatomic, copy) NSString *jobId;
@property(nonatomic, strong) GCFillView *dot;
@property(nonatomic, strong) NSTextField *titleLabel;
@property(nonatomic, strong) NSTextField *detailLabel;
@property(nonatomic) BOOL hovered;
@property(nonatomic) BOOL chosen;
@property(nonatomic, strong) NSTrackingArea *hoverArea;
- (instancetype)initWithJob:(NSDictionary *)job target:(id)target action:(SEL)action;
@end

@implementation OWJobRow
- (instancetype)initWithJob:(NSDictionary *)job target:(id)target action:(SEL)action {
    if (!(self = [super initWithFrame:NSZeroRect])) return nil;
    self.translatesAutoresizingMaskIntoConstraints = NO;
    self.bordered = NO;
    self.title = @"";
    self.target = target;
    self.action = action;
    self.jobId = OWString(job[@"id"]) ?: @"";
    NSString *state = OWString(job[@"state"]) ?: @"";
    NSString *step = OWString(job[@"step"]);
    NSString *server = OWJobServer(job);
    NSMutableArray<NSString *> *details = [NSMutableArray arrayWithObject:state.length ? state.capitalizedString : @"Unknown"];
    if ([step caseInsensitiveCompare:state] == NSOrderedSame) step = nil;  // "Starting · Starting"
    if (step.length && OWStateActive(state)) [details addObject:step];
    else if (step.length && [state isEqualToString:@"failed"]) [details addObject:[@"at " stringByAppendingString:step]];
    if (server.length) [details addObject:server];
    self.dot = [[GCFillView alloc] initWithFrame:NSZeroRect];
    self.dot.cornerRadius = 3.5;
    self.dot.fillColor = OWStateColor(state);
    self.titleLabel = [NSTextField labelWithString:OWJobTitle(job)];
    self.titleLabel.translatesAutoresizingMaskIntoConstraints = NO;
    self.titleLabel.font = GCUIFont(12.5, 600);
    self.titleLabel.textColor = GCTextColor();
    self.titleLabel.lineBreakMode = NSLineBreakByTruncatingTail;
    [self.titleLabel setContentCompressionResistancePriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
    self.detailLabel = [NSTextField labelWithString:[details componentsJoinedByString:@" · "]];
    self.detailLabel.translatesAutoresizingMaskIntoConstraints = NO;
    self.detailLabel.font = GCUIFont(11, 450);
    self.detailLabel.textColor = GCText3Color();
    self.detailLabel.lineBreakMode = NSLineBreakByTruncatingTail;
    [self.detailLabel setContentCompressionResistancePriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
    self.toolTip = [NSString stringWithFormat:@"%@ — %@ (open its log)", self.titleLabel.stringValue, self.detailLabel.stringValue];
    self.accessibilityLabel = self.toolTip;
    [self addSubview:self.dot];
    [self addSubview:self.titleLabel];
    [self addSubview:self.detailLabel];
    [NSLayoutConstraint activateConstraints:@[
        [self.dot.leadingAnchor constraintEqualToAnchor:self.leadingAnchor constant:10],
        [self.dot.centerYAnchor constraintEqualToAnchor:self.titleLabel.centerYAnchor],
        [self.dot.widthAnchor constraintEqualToConstant:7], [self.dot.heightAnchor constraintEqualToConstant:7],
        [self.titleLabel.leadingAnchor constraintEqualToAnchor:self.dot.trailingAnchor constant:9],
        [self.titleLabel.trailingAnchor constraintLessThanOrEqualToAnchor:self.trailingAnchor constant:-8],
        [self.titleLabel.topAnchor constraintEqualToAnchor:self.topAnchor constant:3],
        [self.detailLabel.leadingAnchor constraintEqualToAnchor:self.titleLabel.leadingAnchor],
        [self.detailLabel.trailingAnchor constraintLessThanOrEqualToAnchor:self.trailingAnchor constant:-8],
        [self.detailLabel.topAnchor constraintEqualToAnchor:self.titleLabel.bottomAnchor constant:0],
        [self.heightAnchor constraintEqualToConstant:34],
    ]];
    return self;
}
- (NSView *)hitTest:(NSPoint)point { return [super hitTest:point] ? self : nil; }
- (void)setHovered:(BOOL)hovered { _hovered = hovered; self.needsDisplay = YES; }
- (void)setChosen:(BOOL)chosen {
    _chosen = chosen;
    self.titleLabel.textColor = chosen ? GCAccentTextColor() : GCTextColor();
    self.needsDisplay = YES;
}
- (void)updateTrackingAreas {
    [super updateTrackingAreas];
    if (self.hoverArea) [self removeTrackingArea:self.hoverArea];
    self.hoverArea = [[NSTrackingArea alloc] initWithRect:NSZeroRect
        options:NSTrackingMouseEnteredAndExited | NSTrackingActiveInActiveApp | NSTrackingInVisibleRect owner:self userInfo:nil];
    [self addTrackingArea:self.hoverArea];
}
- (void)mouseEntered:(NSEvent *)event { self.hovered = YES; }
- (void)mouseExited:(NSEvent *)event { self.hovered = NO; }
- (void)viewDidChangeEffectiveAppearance { [super viewDidChangeEffectiveAppearance]; self.chosen = self.chosen; }
- (void)drawRect:(NSRect)dirtyRect {
    if (!self.chosen && !self.hovered) return;
    NSBezierPath *path = [NSBezierPath bezierPathWithRoundedRect:NSInsetRect(self.bounds, 1, 1) xRadius:9 yRadius:9];
    [(self.chosen ? GCAccentSoftColor() : GCHoverColor()) setFill];
    [path fill];
}
@end

@interface OWSplitView : NSSplitView
@end

@implementation OWSplitView
- (NSColor *)dividerColor { return GCDividerColor(); }
@end

#pragma mark - Application

@interface OWAppDelegate : NSObject <NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandler, UNUserNotificationCenterDelegate>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, copy) NSArray<OWSectionCard *> *cards;
@property(nonatomic, strong) OWSectionCard *serversCard;
@property(nonatomic, strong) NSStackView *jobStack;
@property(nonatomic, strong) NSTextField *jobsCountLabel;
@property(nonatomic, strong) NSTextField *jobsEmptyLabel;
@property(nonatomic, strong) GCFillView *statusDot;
@property(nonatomic, strong) NSTextField *statusLabel;
@property(nonatomic, strong) NSTextField *serverLabel;
@property(nonatomic, strong) NSTextField *accountLabel;
@property(nonatomic, strong) NSTextField *pageTitleLabel;
@property(nonatomic, strong) GCButton *backButton;
@property(nonatomic, strong) GCButton *themeButton;
@property(nonatomic, strong) GCButton *reloadButton;
@property(nonatomic, strong) GCFillView *progressBar;
@property(nonatomic, strong) NSLayoutConstraint *progressWidthConstraint;
@property(nonatomic, copy) NSArray<NSMenuItem *> *appearanceMenuItems;
@property(nonatomic, copy) NSArray<NSMenuItem *> *sectionMenuItems;
@property(nonatomic, strong) NSMenuItem *serversMenuItem;
@property(nonatomic, strong) NSMenuItem *notifyMenuItem;
@property(nonatomic, strong) NSMapTable<WKDownload *, NSURL *> *downloadDestinations;
@property(nonatomic, copy) NSString *serverAddress;  // the address pages load from: the server, or the SSH tunnel's local end
@property(nonatomic, strong) OCConnection *connection;
@property(nonatomic) NSInteger tunnelRetries;
@property(nonatomic, copy) NSString *sectionKey;
@property(nonatomic, copy) NSString *currentPath;
/** a page to open once connected (a notification clicked while the app was starting) */
@property(nonatomic, copy) NSString *pendingPath;
@property(nonatomic) BOOL connected;
@property(nonatomic) BOOL snapshotScheduled;
@property(nonatomic) BOOL showingErrorPage;
// jobs and the signed-in account (GET /workbench/api/jobs, /auth/api/me)
@property(nonatomic, strong) NSTimer *pollTimer;
@property(nonatomic, strong) NSDate *lastPoll;
@property(nonatomic) BOOL polling;
@property(nonatomic) BOOL fixtureLoaded;
@property(nonatomic, copy) NSArray<NSDictionary *> *jobs;
@property(nonatomic, copy) NSString *user;
@property(nonatomic) BOOL admin;
@property(nonatomic, strong) id<NSObject> watchActivity;
@property(nonatomic, copy) NSDictionary *snapshotJobStates;  // snapshot runs keep the seen states in memory only
@end

@implementation OWAppDelegate

- (void)applicationWillFinishLaunching:(NSNotification *)notification {
    // set before launching ends, so a click on a notification that started the app reaches this delegate
    if (NSBundle.mainBundle.bundleIdentifier.length) [UNUserNotificationCenter currentNotificationCenter].delegate = self;
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    GCRegisterBundledFonts();
    self.downloadDestinations = [NSMapTable weakToStrongObjectsMapTable];
    self.jobs = @[];
    NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
    // server and port, optionally through SSH port forwarding (Server…), as in Omics Canvas
    self.connection = [OCConnection connectionWithDefaultsPrefix:@"OmicsWorkbenchConnection" defaultAddress:OWDefaultServerAddress legacyAddress:nil];
    __weak OWAppDelegate *weakSelf = self;
    self.connection.onTunnelClosed = ^(NSString *message) { [weakSelf tunnelClosed:message]; };
    self.serverAddress = self.connection.useTunnel ? @"" : self.connection.directURL.absoluteString;
    NSString *saved = [defaults stringForKey:OWSectionDefaultsKey];
    // Servers is shown only once the account is known to be an administrator
    self.sectionKey = OWSectionForKey([saved isEqualToString:@"servers"] ? @"overview" : saved)[@"key"];
    [self buildApplicationMenu];

    NSRect frame = NSMakeRect(0, 0, 1400, 900);
    NSWindowStyleMask style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable;
    self.window = [[NSWindow alloc] initWithContentRect:frame styleMask:style backing:NSBackingStoreBuffered defer:NO];
    self.window.releasedWhenClosed = NO;  // closing the window keeps the app (and the job notifications) running
    self.window.title = @"Omics Workbench";
    self.window.subtitle = @"Download · bulk · single-cell";
    self.window.minSize = NSMakeSize(1000, 720);
    self.window.titlebarAppearsTransparent = YES;
    self.window.backgroundColor = GCBackgroundColor();
    self.window.frameAutosaveName = @"OmicsWorkbenchMainWindow";
    [self.window center];

    OWSplitView *splitView = [[OWSplitView alloc] initWithFrame:frame];
    splitView.translatesAutoresizingMaskIntoConstraints = NO;
    splitView.vertical = YES;
    splitView.dividerStyle = NSSplitViewDividerStyleThin;
    self.window.contentView = splitView;
    NSView *sidebar = [self buildSidebar];
    NSView *content = [self buildContent];
    [splitView addArrangedSubview:sidebar];
    [splitView addArrangedSubview:content];
    [sidebar.widthAnchor constraintEqualToConstant:270].active = YES;

    [self.webView addObserver:self forKeyPath:@"estimatedProgress" options:NSKeyValueObservingOptionInitial | NSKeyValueObservingOptionNew context:NULL];
    [self.webView addObserver:self forKeyPath:@"loading" options:NSKeyValueObservingOptionNew context:NULL];
    [self.webView addObserver:self forKeyPath:@"canGoBack" options:NSKeyValueObservingOptionInitial | NSKeyValueObservingOptionNew context:NULL];
    [NSApp addObserver:self forKeyPath:@"effectiveAppearance" options:NSKeyValueObservingOptionNew context:NULL];
    [self applyAppearancePreference];
    [self selectSection:self.sectionKey];
    [self updateAccount];
    [self showJobs:@[]];

    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    [self updateServerLabel];
    [self connectAndShowSection];

    if (![self snapshotDirectory].length) {
        if ([self notificationsEnabled]) [self requestNotificationPermission];
        NSTimer *timer = [NSTimer timerWithTimeInterval:OWPollInterval repeats:YES block:^(NSTimer *t) { [weakSelf pollJobs]; }];
        timer.tolerance = 5;
        // common modes: also while a menu is open or a sheet is up
        [[NSRunLoop mainRunLoop] addTimer:timer forMode:NSRunLoopCommonModes];
        self.pollTimer = timer;
    }
}

/** Open the connection (starting the SSH tunnel when one is configured), then the current page. */
- (void)connectAndShowSection {
    self.connected = NO;
    if (self.connection.useTunnel) [self setStatus:@"Opening SSH port forwarding" state:@"busy"];
    [self.connection openWithCompletion:^(NSURL *url, NSString *error) {
        if (!url) { [self showConnectionError:error]; return; }
        self.tunnelRetries = 0;
        self.serverAddress = url.absoluteString;
        self.connected = YES;
        NSLog(@"connected: %@ (%@)", url.absoluteString, self.connection.summary);
        [self updateServerLabel];
        if (self.pendingPath.length) {
            NSString *path = self.pendingPath;
            self.pendingPath = nil;
            [self loadPage:path];
        } else {
            [self showSection:self.sectionKey];
        }
    }];
}

/** The tunnel dropped (network change, sleep): reopen it on the same local port, so the open page carries on. */
- (void)tunnelClosed:(NSString *)message {
    if (self.tunnelRetries >= 3) { [self showConnectionError:[NSString stringWithFormat:@"SSH port forwarding closed: %@", message]]; return; }
    self.tunnelRetries += 1;
    [self setStatus:@"Reconnecting SSH port forwarding" state:@"busy"];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [self.connection openWithCompletion:^(NSURL *url, NSString *error) {
            if (!url) { [self showConnectionError:error]; return; }
            self.tunnelRetries = 0;
            self.connected = YES;
            if (![url.absoluteString isEqualToString:self.serverAddress]) { self.serverAddress = url.absoluteString; [self showSection:self.sectionKey]; }
            [self setStatus:@"Connected" state:@"ready"];
        }];
    });
}

- (void)showConnectionError:(NSString *)message {
    NSLog(@"not connected: %@ (%@)", message, self.connection.summary);
    self.connected = NO;
    [self setStatus:@"Not connected" state:@"error"];
    self.statusLabel.toolTip = message;
    NSString *html = [NSString stringWithFormat:
        @"<!doctype html><meta charset=utf-8><style>:root{color-scheme:light dark;--bg:#e5e9f0;--text:#2e3440;--muted:#69728a}"
        "@media (prefers-color-scheme:dark){:root{--bg:#242933;--text:#eceff4;--muted:#9aa5b8}}"
        "html,body{margin:0;height:100%%;background:var(--bg);color:var(--text);font:14px -apple-system,sans-serif}"
        "main{height:100%%;display:grid;place-content:center;text-align:center;gap:6px;padding:24px}h1{font:600 19px Georgia,serif;margin:0 0 4px}"
        "p{margin:0;color:var(--muted);max-width:460px}code{font:12px ui-monospace,monospace}</style>"
        "<main><h1>Could not connect</h1><p><code>%@</code></p><p>%@</p>"
        "<p>Check the address, port and SSH account under <b>Server…</b> in the sidebar.</p></main>",
        [self htmlEscape:self.connection.summary], [self htmlEscape:message ?: @""]];
    self.showingErrorPage = YES;
    [self.webView loadHTMLString:html baseURL:nil];
    [self scheduleSnapshotCaptureIfRequested];
}

- (NSString *)htmlEscape:(NSString *)text {
    NSMutableString *out = [NSMutableString stringWithCapacity:text.length];
    for (NSUInteger i = 0; i < text.length; i++) {
        unichar c = [text characterAtIndex:i];
        if (c == '<') [out appendString:@"&lt;"]; else if (c == '>') [out appendString:@"&gt;"]; else if (c == '&') [out appendString:@"&amp;"];
        else [out appendFormat:@"%C", c];
    }
    return out;
}

#pragma mark Sidebar

- (NSView *)buildSidebar {
    GCFillView *sidebar = [[GCFillView alloc] initWithFrame:NSZeroRect];
    sidebar.fillColor = GCRailColor();

    NSImageView *mark = [NSImageView imageViewWithImage:NSApp.applicationIconImage ?: [NSImage imageNamed:NSImageNameApplicationIcon]];
    mark.translatesAutoresizingMaskIntoConstraints = NO;
    mark.imageScaling = NSImageScaleProportionallyUpOrDown;
    NSTextField *title = [NSTextField labelWithString:@"Omics Workbench"];
    title.font = GCHeadingFont(16);
    title.textColor = GCTextColor();
    NSStackView *brand = [NSStackView stackViewWithViews:@[mark, title]];
    brand.orientation = NSUserInterfaceLayoutOrientationHorizontal;
    brand.alignment = NSLayoutAttributeCenterY;
    brand.spacing = 9;

    NSTextField *sectionsLabel = [self sectionLabel:@"Analysis"];
    NSMutableArray<OWSectionCard *> *cards = [NSMutableArray array];
    for (NSDictionary<NSString *, NSString *> *section in OWSections()) {
        OWSectionCard *card = [[OWSectionCard alloc] initWithSection:section target:self action:@selector(chooseSection:)];
        if ([section[@"key"] isEqualToString:@"servers"]) { card.hidden = YES; self.serversCard = card; }
        [cards addObject:card];
    }
    self.cards = cards;
    NSStackView *cardStack = [NSStackView stackViewWithViews:cards];
    cardStack.orientation = NSUserInterfaceLayoutOrientationVertical;
    cardStack.spacing = 4;

    NSView *divider = [self dividerLine];

    NSTextField *jobsLabel = [self sectionLabel:@"Recent jobs"];
    self.jobsCountLabel = [NSTextField labelWithString:@""];
    self.jobsCountLabel.font = GCUIFont(11.5, 550);
    self.jobsCountLabel.textColor = GCText3Color();
    GCButton *refreshButton = [self iconButton:@"arrow.clockwise" tip:@"Check the jobs now" action:@selector(checkJobsNow:)];
    refreshButton.fixedHeight = 26;
    NSView *headingSpacer = [[NSView alloc] initWithFrame:NSZeroRect];
    [headingSpacer setContentHuggingPriority:1 forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSStackView *jobsHeading = [NSStackView stackViewWithViews:@[jobsLabel, self.jobsCountLabel, headingSpacer, refreshButton]];
    jobsHeading.orientation = NSUserInterfaceLayoutOrientationHorizontal;
    jobsHeading.alignment = NSLayoutAttributeCenterY;
    jobsHeading.spacing = 6;

    self.jobStack = [NSStackView stackViewWithViews:@[]];
    self.jobStack.translatesAutoresizingMaskIntoConstraints = NO;
    self.jobStack.orientation = NSUserInterfaceLayoutOrientationVertical;
    self.jobStack.alignment = NSLayoutAttributeLeading;
    self.jobStack.spacing = 2;
    self.jobsEmptyLabel = [NSTextField wrappingLabelWithString:@"No jobs yet. Start one from Download data, Bulk sequencing or Single-cell."];
    self.jobsEmptyLabel.font = GCUIFont(12, 450);
    self.jobsEmptyLabel.textColor = GCText3Color();

    NSView *flexible = [[NSView alloc] initWithFrame:NSZeroRect];
    flexible.translatesAutoresizingMaskIntoConstraints = NO;
    [flexible setContentHuggingPriority:1 forOrientation:NSLayoutConstraintOrientationVertical];
    [flexible setContentCompressionResistancePriority:1 forOrientation:NSLayoutConstraintOrientationVertical];

    self.statusDot = [[GCFillView alloc] initWithFrame:NSZeroRect];
    self.statusDot.cornerRadius = 3.5;
    self.statusDot.fillColor = GCWarningColor();
    self.statusLabel = [NSTextField labelWithString:@"Connecting"];
    self.statusLabel.font = GCUIFont(12, 550);
    self.statusLabel.textColor = GCText2Color();
    self.statusLabel.lineBreakMode = NSLineBreakByTruncatingTail;
    NSStackView *status = [NSStackView stackViewWithViews:@[self.statusDot, self.statusLabel]];
    status.orientation = NSUserInterfaceLayoutOrientationHorizontal;
    status.alignment = NSLayoutAttributeCenterY;
    status.spacing = 7;
    self.serverLabel = [NSTextField labelWithString:@""];
    self.serverLabel.font = GCMonoFont(10.5, 500);
    self.serverLabel.textColor = GCText4Color();
    self.serverLabel.lineBreakMode = NSLineBreakByTruncatingMiddle;
    self.accountLabel = [NSTextField labelWithString:@""];
    self.accountLabel.font = GCUIFont(11, 500);
    self.accountLabel.textColor = GCText3Color();
    self.accountLabel.lineBreakMode = NSLineBreakByTruncatingTail;
    GCButton *serverButton = [GCButton buttonWithLabel:@"Server…" symbol:@"server.rack" style:GCButtonStyleGhost target:self action:@selector(editServerAddress:)];
    serverButton.font = GCUIFont(12, 550);
    serverButton.label = serverButton.label;
    serverButton.fixedHeight = 26;
    serverButton.toolTip = @"Connect to another server address";
    NSView *footerSpacer = [[NSView alloc] initWithFrame:NSZeroRect];
    [footerSpacer setContentHuggingPriority:1 forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSStackView *footer = [NSStackView stackViewWithViews:@[status, footerSpacer, serverButton]];
    footer.orientation = NSUserInterfaceLayoutOrientationHorizontal;
    footer.alignment = NSLayoutAttributeCenterY;

    NSArray<NSView *> *rows = @[brand, sectionsLabel, cardStack, divider, jobsHeading, self.jobsEmptyLabel, self.jobStack, flexible, footer, self.serverLabel, self.accountLabel];
    NSStackView *columnStack = [NSStackView stackViewWithViews:rows];
    columnStack.translatesAutoresizingMaskIntoConstraints = NO;
    columnStack.orientation = NSUserInterfaceLayoutOrientationVertical;
    columnStack.alignment = NSLayoutAttributeLeading;
    columnStack.distribution = NSStackViewDistributionFill;
    columnStack.spacing = 6;
    [columnStack setCustomSpacing:18 afterView:brand];
    [columnStack setCustomSpacing:16 afterView:cardStack];
    [columnStack setCustomSpacing:12 afterView:divider];
    [columnStack setCustomSpacing:4 afterView:jobsHeading];
    [columnStack setCustomSpacing:2 afterView:footer];
    [columnStack setCustomSpacing:2 afterView:self.serverLabel];
    [sidebar addSubview:columnStack];

    NSMutableArray<NSLayoutConstraint *> *constraints = [NSMutableArray arrayWithArray:@[
        [mark.widthAnchor constraintEqualToConstant:28], [mark.heightAnchor constraintEqualToConstant:28],
        [columnStack.topAnchor constraintEqualToAnchor:sidebar.safeAreaLayoutGuide.topAnchor constant:12],
        [columnStack.leadingAnchor constraintEqualToAnchor:sidebar.leadingAnchor constant:12],
        [columnStack.trailingAnchor constraintEqualToAnchor:sidebar.trailingAnchor constant:-12],
        [columnStack.bottomAnchor constraintEqualToAnchor:sidebar.bottomAnchor constant:-12],
        [self.statusDot.widthAnchor constraintEqualToConstant:7], [self.statusDot.heightAnchor constraintEqualToConstant:7],
    ]];
    for (NSView *row in @[cardStack, divider, jobsHeading, self.jobsEmptyLabel, self.jobStack, flexible, footer, self.serverLabel, self.accountLabel]) {
        [constraints addObject:[row.widthAnchor constraintEqualToAnchor:columnStack.widthAnchor]];
    }
    for (OWSectionCard *card in cards) [constraints addObject:[card.widthAnchor constraintEqualToAnchor:cardStack.widthAnchor]];
    [NSLayoutConstraint activateConstraints:constraints];
    return sidebar;
}

#pragma mark Toolbar and page

- (NSView *)buildContent {
    NSView *content = [[NSView alloc] initWithFrame:NSZeroRect];
    content.translatesAutoresizingMaskIntoConstraints = NO;
    GCFillView *toolbar = [[GCFillView alloc] initWithFrame:NSZeroRect];
    toolbar.fillColor = GCSurfaceColor();
    [content addSubview:toolbar];
    GCFillView *toolbarRule = [[GCFillView alloc] initWithFrame:NSZeroRect];
    toolbarRule.fillColor = GCBorderColor();
    [content addSubview:toolbarRule];

    self.backButton = [self toolButton:@"chevron.left" tip:@"Back" action:@selector(goBack:)];
    GCButton *jobsButton = [GCButton buttonWithLabel:@"Jobs" symbol:@"list.bullet.rectangle" style:GCButtonStyleSecondary target:self action:@selector(showJobsPage:)];
    jobsButton.toolTip = @"Your jobs and their logs";
    jobsButton.fixedHeight = 32;
    self.pageTitleLabel = [NSTextField labelWithString:@""];
    self.pageTitleLabel.font = GCHeadingFont(15);
    self.pageTitleLabel.textColor = GCTextColor();
    self.pageTitleLabel.lineBreakMode = NSLineBreakByTruncatingTail;
    [self.pageTitleLabel setContentCompressionResistancePriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSView *spacer = [[NSView alloc] initWithFrame:NSZeroRect];
    [spacer setContentHuggingPriority:1 forOrientation:NSLayoutConstraintOrientationHorizontal];
    GCButton *browserButton = [self toolButton:@"safari" tip:@"Open this page in the web browser" action:@selector(openInBrowser:)];
    self.themeButton = [self iconButton:@"moon" tip:@"Switch to dark theme" action:@selector(toggleAppearance:)];
    self.reloadButton = [self iconButton:@"arrow.clockwise" tip:@"Reload" action:@selector(reloadOrStop:)];
    for (GCButton *button in @[self.themeButton, self.reloadButton]) button.fixedHeight = 34;

    NSStackView *controls = [NSStackView stackViewWithViews:@[
        self.backButton, jobsButton, [self verticalRule], self.pageTitleLabel, spacer,
        browserButton, [self verticalRule], self.themeButton, self.reloadButton
    ]];
    controls.translatesAutoresizingMaskIntoConstraints = NO;
    controls.orientation = NSUserInterfaceLayoutOrientationHorizontal;
    controls.alignment = NSLayoutAttributeCenterY;
    controls.spacing = 6;
    [controls setCustomSpacing:10 afterView:jobsButton];
    [toolbar addSubview:controls];

    GCFillView *progressTrack = [[GCFillView alloc] initWithFrame:NSZeroRect];
    [content addSubview:progressTrack];
    self.progressBar = [[GCFillView alloc] initWithFrame:NSZeroRect];
    self.progressBar.fillColor = GCAccentColor();
    self.progressBar.hidden = YES;
    [progressTrack addSubview:self.progressBar];
    self.progressWidthConstraint = [self.progressBar.widthAnchor constraintEqualToConstant:0];

    WKWebViewConfiguration *configuration = [[WKWebViewConfiguration alloc] init];
    configuration.websiteDataStore = [WKWebsiteDataStore defaultDataStore];
    configuration.preferences.javaScriptCanOpenWindowsAutomatically = YES;
    [configuration.userContentController addScriptMessageHandler:self name:@"vizhub"];
    self.webView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
    self.webView.translatesAutoresizingMaskIntoConstraints = NO;
    self.webView.navigationDelegate = self;
    self.webView.UIDelegate = self;
    self.webView.allowsMagnification = YES;
    self.webView.allowsBackForwardNavigationGestures = YES;
    self.webView.underPageBackgroundColor = GCBackgroundColor();

    GCFillView *pageCard = [[GCFillView alloc] initWithFrame:NSZeroRect];
    pageCard.fillColor = GCBackgroundColor();
    pageCard.strokeColor = GCBorderColor();
    pageCard.cornerRadius = 13;
    pageCard.clipsContent = YES;
    [pageCard addSubview:self.webView];
    [content addSubview:pageCard];

    [NSLayoutConstraint activateConstraints:@[
        [toolbar.topAnchor constraintEqualToAnchor:content.topAnchor],
        [toolbar.leadingAnchor constraintEqualToAnchor:content.leadingAnchor],
        [toolbar.trailingAnchor constraintEqualToAnchor:content.trailingAnchor],
        [toolbar.heightAnchor constraintEqualToConstant:56],
        [toolbarRule.topAnchor constraintEqualToAnchor:toolbar.bottomAnchor],
        [toolbarRule.leadingAnchor constraintEqualToAnchor:content.leadingAnchor],
        [toolbarRule.trailingAnchor constraintEqualToAnchor:content.trailingAnchor],
        [toolbarRule.heightAnchor constraintEqualToConstant:1],
        [controls.leadingAnchor constraintEqualToAnchor:toolbar.leadingAnchor constant:12],
        [controls.trailingAnchor constraintEqualToAnchor:toolbar.trailingAnchor constant:-12],
        [controls.centerYAnchor constraintEqualToAnchor:toolbar.centerYAnchor],
        [progressTrack.topAnchor constraintEqualToAnchor:toolbarRule.bottomAnchor],
        [progressTrack.leadingAnchor constraintEqualToAnchor:content.leadingAnchor],
        [progressTrack.trailingAnchor constraintEqualToAnchor:content.trailingAnchor],
        [progressTrack.heightAnchor constraintEqualToConstant:2],
        [self.progressBar.leadingAnchor constraintEqualToAnchor:progressTrack.leadingAnchor],
        [self.progressBar.topAnchor constraintEqualToAnchor:progressTrack.topAnchor],
        [self.progressBar.bottomAnchor constraintEqualToAnchor:progressTrack.bottomAnchor],
        self.progressWidthConstraint,
        [pageCard.topAnchor constraintEqualToAnchor:progressTrack.bottomAnchor constant:8],
        [pageCard.leadingAnchor constraintEqualToAnchor:content.leadingAnchor constant:10],
        [pageCard.trailingAnchor constraintEqualToAnchor:content.trailingAnchor constant:-10],
        [pageCard.bottomAnchor constraintEqualToAnchor:content.bottomAnchor constant:-10],
        [self.webView.topAnchor constraintEqualToAnchor:pageCard.topAnchor constant:1],
        [self.webView.leadingAnchor constraintEqualToAnchor:pageCard.leadingAnchor constant:1],
        [self.webView.trailingAnchor constraintEqualToAnchor:pageCard.trailingAnchor constant:-1],
        [self.webView.bottomAnchor constraintEqualToAnchor:pageCard.bottomAnchor constant:-1],
    ]];
    return content;
}

#pragma mark Control factories (as in Omics Canvas)

- (NSTextField *)sectionLabel:(NSString *)text {
    NSTextField *label = [NSTextField labelWithString:@""];
    label.translatesAutoresizingMaskIntoConstraints = NO;
    label.attributedStringValue = [[NSAttributedString alloc] initWithString:text.uppercaseString attributes:@{
        NSFontAttributeName: GCUIFont(11, 650),
        NSForegroundColorAttributeName: GCText3Color(),
        NSKernAttributeName: @0.7,
    }];
    return label;
}

- (NSView *)dividerLine {
    GCFillView *line = [[GCFillView alloc] initWithFrame:NSZeroRect];
    line.fillColor = GCBorderColor();
    [line.heightAnchor constraintEqualToConstant:1].active = YES;
    return line;
}

- (NSView *)verticalRule {
    GCFillView *line = [[GCFillView alloc] initWithFrame:NSZeroRect];
    line.fillColor = GCBorderColor();
    [line.widthAnchor constraintEqualToConstant:1].active = YES;
    [line.heightAnchor constraintEqualToConstant:22].active = YES;
    return line;
}

- (GCButton *)iconButton:(NSString *)symbol tip:(NSString *)tip action:(SEL)action {
    GCButton *button = [GCButton buttonWithLabel:nil symbol:symbol style:GCButtonStyleGhost target:self action:action];
    button.toolTip = tip;
    button.fixedHeight = 30;
    return button;
}

- (GCButton *)toolButton:(NSString *)symbol tip:(NSString *)tip action:(SEL)action {
    GCButton *button = [GCButton buttonWithLabel:nil symbol:symbol style:GCButtonStyleTool target:self action:action];
    button.toolTip = tip;
    button.fixedHeight = 32;
    return button;
}

#pragma mark Server address and pages

- (NSURL *)serverRoot {
    NSString *candidate = [self.serverAddress stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    if (!candidate.length) candidate = OWDefaultServerAddress;
    if ([candidate rangeOfString:@"://"].location == NSNotFound) candidate = [@"http://" stringByAppendingString:candidate];
    NSURLComponents *components = [NSURLComponents componentsWithString:candidate];
    NSString *scheme = components.scheme.lowercaseString;
    if (!components.host.length || !([scheme isEqualToString:@"http"] || [scheme isEqualToString:@"https"])) return nil;
    components.path = @"/";
    components.query = nil;
    components.fragment = nil;
    return components.URL;
}

/** A page of the server, e.g. "/workbench/bulk/" or "/admin/#servers", opened in desktop mode. */
- (NSURL *)pageURL:(NSString *)pathAndQuery {
    NSURL *root = [self serverRoot];
    if (!root) return nil;
    NSURLComponents *page = [NSURLComponents componentsWithString:pathAndQuery];
    NSURLComponents *components = [NSURLComponents componentsWithURL:root resolvingAgainstBaseURL:NO];
    components.path = page.path.length ? page.path : @"/";
    NSMutableArray<NSURLQueryItem *> *items = [NSMutableArray array];
    for (NSURLQueryItem *item in page.queryItems ?: @[]) if (![item.name isEqualToString:@"desktop"]) [items addObject:item];
    [items addObject:[NSURLQueryItem queryItemWithName:@"desktop" value:@"1"]];
    components.queryItems = items;
    components.fragment = page.fragment;
    return components.URL;
}

- (void)loadPage:(NSString *)pathAndQuery {
    NSURL *url = [self pageURL:pathAndQuery];
    if (!url) { [self setStatus:@"Invalid server address" state:@"error"]; NSBeep(); return; }
    [self setStatus:@"Connecting" state:@"busy"];
    [self.webView loadRequest:[NSURLRequest requestWithURL:url cachePolicy:NSURLRequestReloadRevalidatingCacheData timeoutInterval:30]];
}

/** Open a page now, or once the connection is up (a notification clicked while the app was still starting). */
- (void)openPathWhenConnected:(NSString *)path {
    if (self.connected) [self loadPage:path];
    else self.pendingPath = path;
}

- (BOOL)isSameServer:(NSURL *)url {
    NSURL *root = [self serverRoot];
    return root && url.host && [url.host isEqualToString:root.host] && ((url.port == nil && root.port == nil) || [url.port isEqual:root.port]);
}

- (void)updateServerLabel {
    self.serverLabel.stringValue = self.connection.summary;
    self.serverLabel.toolTip = self.connection.useTunnel
        ? [NSString stringWithFormat:@"SSH port forwarding: pages load from %@", [self serverRoot].absoluteString ?: @"(not connected)"]
        : self.connection.directURL.absoluteString;
}

- (void)selectSection:(NSString *)key {
    for (OWSectionCard *card in self.cards) card.chosen = [card.key isEqualToString:key];
    for (NSMenuItem *item in self.sectionMenuItems) item.state = [item.representedObject isEqualToString:key] ? NSControlStateValueOn : NSControlStateValueOff;
}

/** Open a page of the sidebar. */
- (void)showSection:(NSString *)key {
    NSDictionary<NSString *, NSString *> *section = OWSectionForKey(key);
    self.sectionKey = section[@"key"];
    [[NSUserDefaults standardUserDefaults] setObject:self.sectionKey forKey:OWSectionDefaultsKey];
    [self selectSection:self.sectionKey];
    [self loadPage:section[@"page"]];
}

- (void)chooseSection:(OWSectionCard *)sender { [self showSection:sender.key]; }
- (void)chooseSectionFromMenu:(NSMenuItem *)sender { [self showSection:sender.representedObject]; }
- (void)showJobsPage:(id)sender { [self showSection:@"jobs"]; }
- (void)goBack:(id)sender { if (self.webView.canGoBack) [self.webView goBack]; }

- (void)openInBrowser:(id)sender {
    NSURLComponents *components = [NSURLComponents componentsWithURL:self.webView.URL ?: [self serverRoot] resolvingAgainstBaseURL:NO];
    NSMutableArray<NSURLQueryItem *> *items = [NSMutableArray array];
    for (NSURLQueryItem *item in components.queryItems ?: @[]) if (![item.name isEqualToString:@"desktop"]) [items addObject:item];
    components.queryItems = items.count ? items : nil;
    if (components.URL) [[NSWorkspace sharedWorkspace] openURL:components.URL];
}

// the server's sign-in (auth/): end this session; the sign-in page opens in the window and leads back to this page
- (void)signOut:(id)sender {
    NSURL *root = [self serverRoot];
    if (!root) { NSBeep(); return; }
    NSURLComponents *page = self.webView.URL ? [NSURLComponents componentsWithURL:self.webView.URL resolvingAgainstBaseURL:NO] : nil;
    NSString *next = OWSectionForKey(self.sectionKey)[@"page"];
    if (page && OWSectionForPath(page.path ?: @"")) next = page.percentEncodedQuery.length ? [NSString stringWithFormat:@"%@?%@", page.percentEncodedPath, page.percentEncodedQuery] : page.percentEncodedPath;
    NSURLComponents *components = [NSURLComponents componentsWithURL:root resolvingAgainstBaseURL:NO];
    components.path = @"/auth/logout";
    components.queryItems = @[[NSURLQueryItem queryItemWithName:@"next" value:next]];
    [self setStatus:@"Signing out" state:@"busy"];
    // signed out: no account, no jobs until the next sign-in
    self.user = nil;
    self.admin = NO;
    [self updateAccount];
    [self showJobs:@[]];
    [self.webView loadRequest:[NSURLRequest requestWithURL:components.URL]];
}

- (void)editServerAddress:(id)sender {
    [self.connection editInWindow:self.window title:@"Connect to an Omics Studio Server"
                          message:@"The gateway's address and port (default 171.65.68.140:8892, the lab server THOR). From outside Stanford connect the VPN — or reach the server through SSH port forwarding with your account on it."
                             done:^(BOOL connect) {
        if (!connect) return;
        self.user = nil;
        self.admin = NO;
        [self updateAccount];
        [self showJobs:@[]];
        [self updateServerLabel];
        [self connectAndShowSection];
    }];
}

- (void)setStatus:(NSString *)text state:(NSString *)state {
    self.statusLabel.stringValue = text.length ? text : @"Ready";
    if ([state isEqualToString:@"error"]) self.statusDot.fillColor = GCDangerColor();
    else if ([state isEqualToString:@"busy"]) self.statusDot.fillColor = GCWarningColor();
    else self.statusDot.fillColor = GCSuccessColor();
}

#pragma mark Bridge to the page (window.VizHubDesktop in hub/web/src/common.ts)

- (NSString *)JSONFragment:(id)value {
    NSData *data = [NSJSONSerialization dataWithJSONObject:value ?: [NSNull null] options:NSJSONWritingFragmentsAllowed error:nil];
    return data ? [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] : @"null";
}

- (void)callDesktopMethod:(NSString *)method arguments:(NSArray *)arguments completion:(void (^)(id result))completion {
    NSMutableArray<NSString *> *encoded = [NSMutableArray arrayWithCapacity:arguments.count];
    for (id argument in arguments) [encoded addObject:[self JSONFragment:argument]];
    // callAsyncJavaScript awaits the promise the bridge method returns
    NSString *body = [NSString stringWithFormat:@"const b = window.VizHubDesktop; if (!b || !b[%@]) return null; return await b[%@](%@);",
        [self JSONFragment:method], [self JSONFragment:method], [encoded componentsJoinedByString:@","]];
    [self.webView callAsyncJavaScript:body arguments:@{} inFrame:nil inContentWorld:[WKContentWorld pageWorld] completionHandler:^(id result, NSError *error) {
        if (completion) completion(error || [result isKindOfClass:[NSNull class]] ? nil : result);
    }];
}

- (void)userContentController:(WKUserContentController *)userContentController didReceiveScriptMessage:(WKScriptMessage *)message {
    if (![message.name isEqualToString:@"vizhub"] || ![message.body isKindOfClass:[NSDictionary class]]) return;
    NSDictionary *payload = message.body;
    NSString *type = payload[@"type"];
    if ([type isEqualToString:@"page"]) {
        self.currentPath = [payload[@"path"] isKindOfClass:[NSString class]] ? payload[@"path"] : @"";
        [self followPath:[self.currentPath componentsSeparatedByString:@"?"].firstObject];
        [self pollJobsSoon];
    } else if ([type isEqualToString:@"workbench"]) {
        // a workbench page opened (view, job): the jobs may have changed since the last poll
        [self pollJobsSoon];
    } else if ([type isEqualToString:@"jobs"]) {
        // a page started, cancelled or removed a job: show it now rather than at the next poll
        self.lastPoll = nil;
        [self pollJobs];
    }
}

/** The page showing (from its URL or its "page" message): its card in the sidebar, its job row. */
- (void)followPath:(NSString *)path {
    NSDictionary<NSString *, NSString *> *section = OWSectionForPath(path ?: @"");
    if (section && ![section[@"key"] isEqualToString:self.sectionKey]) {
        self.sectionKey = section[@"key"];
        if (![self.sectionKey isEqualToString:@"servers"]) [[NSUserDefaults standardUserDefaults] setObject:self.sectionKey forKey:OWSectionDefaultsKey];
    }
    // no page yet (or the "not reachable" page): the card chosen last stays chosen; other pages (sign-in) choose none
    if (path.length) [self selectSection:section ? self.sectionKey : @""];
    for (NSView *view in self.jobStack.arrangedSubviews) {
        if ([view isKindOfClass:[OWJobRow class]]) [(OWJobRow *)view setChosen:[(path ?: @"") hasPrefix:OWJobPath([(OWJobRow *)view jobId])]];
    }
}

#pragma mark Jobs: polling, sidebar, Dock badge, notifications

- (BOOL)pageIsOnServer {
    NSURL *url = self.webView.URL;
    NSString *scheme = url.scheme.lowercaseString;
    return url && ([scheme isEqualToString:@"http"] || [scheme isEqualToString:@"https"]) && [self isSameServer:url];
}

/** GET <path> from the page showing, so it goes with the page's sign-in session (cookie); status 0: no answer,
    -1: no server page is showing (the "not reachable" page, for one). */
- (void)fetchJSON:(NSString *)path completion:(void (^)(NSInteger status, id json))completion {
    if (![self pageIsOnServer]) { completion(-1, nil); return; }
    NSString *body = @"try {"
        " const r = await fetch(path, {credentials: 'same-origin', cache: 'no-store', headers: {accept: 'application/json'}});"
        " let data = null;"
        " try { data = await r.json(); } catch (e) { data = null; }"
        " return {status: r.status, data: data};"
        "} catch (e) { return {status: 0, data: null}; }";
    [self.webView callAsyncJavaScript:body arguments:@{@"path": path} inFrame:nil inContentWorld:[WKContentWorld pageWorld] completionHandler:^(id result, NSError *error) {
        NSDictionary *response = !error && [result isKindOfClass:[NSDictionary class]] ? result : nil;
        NSInteger status = [response[@"status"] isKindOfClass:[NSNumber class]] ? [response[@"status"] integerValue] : 0;
        id data = response[@"data"];
        completion(status, [data isKindOfClass:[NSNull class]] ? nil : data);
    }];
}

- (void)pollJobsSoon {
    if (self.lastPoll && -[self.lastPoll timeIntervalSinceNow] < 5) return;
    [self pollJobs];
}

- (void)checkJobsNow:(id)sender {
    self.lastPoll = nil;
    self.polling = NO;
    [self pollJobs];
}

/** Who is signed in (admin: the Servers page), then the jobs. */
- (void)pollJobs {
    if (self.fixtureLoaded) return;
    if (self.polling && self.lastPoll && -[self.lastPoll timeIntervalSinceNow] < 60) return;
    if (![self pageIsOnServer]) return;
    self.polling = YES;
    self.lastPoll = [NSDate date];
    [self fetchJSON:@"/auth/api/me" completion:^(NSInteger status, id me) {
        [self applyIdentity:[me isKindOfClass:[NSDictionary class]] ? me : nil status:status];
        [self fetchJSON:@"/workbench/api/jobs" completion:^(NSInteger jobsStatus, id payload) {
            self.polling = NO;
            if (jobsStatus == 401 || jobsStatus == 403) { [self showJobs:@[]]; return; }  // signed out
            if (jobsStatus != 200 || ![payload isKindOfClass:[NSDictionary class]] || ![payload[@"jobs"] isKindOfClass:[NSArray class]]) return;
            [self processJobs:payload[@"jobs"] silently:NO];
        }];
    }];
}

- (void)applyIdentity:(NSDictionary *)me status:(NSInteger)status {
    if (status <= 0) return;  // no answer: keep what was known
    BOOL signedIn = status == 200 && me != nil;
    self.user = signedIn ? OWString(me[@"user"]) : nil;
    self.admin = signedIn && [me[@"admin"] respondsToSelector:@selector(boolValue)] && [me[@"admin"] boolValue];
    [self updateAccount];
}

- (void)updateAccount {
    self.serversCard.hidden = !self.admin;
    self.serversMenuItem.hidden = !self.admin;
    self.accountLabel.stringValue = self.user.length ? [NSString stringWithFormat:@"Signed in as %@%@", self.user, self.admin ? @" · administrator" : @""] : @"";
    self.accountLabel.hidden = !self.user.length;
}

/** "<server> <user>": the seen states are kept per server and account */
- (NSString *)jobStatesKey {
    return [NSString stringWithFormat:@"%@:%ld %@", self.connection.host.lowercaseString ?: @"", (long)self.connection.port, self.user ?: @""];
}

- (NSDictionary *)allJobStates {
    if ([self snapshotDirectory].length) return self.snapshotJobStates;
    return [[NSUserDefaults standardUserDefaults] dictionaryForKey:OWJobStatesDefaultsKey];
}

- (void)saveJobStates:(NSDictionary<NSString *, NSString *> *)states {
    NSMutableDictionary *all = [[self allJobStates] mutableCopy] ?: [NSMutableDictionary dictionary];
    all[[self jobStatesKey]] = states;
    if ([self snapshotDirectory].length) self.snapshotJobStates = all;
    else [[NSUserDefaults standardUserDefaults] setObject:all forKey:OWJobStatesDefaultsKey];
}

/** Compare with the states seen before: announce the jobs that have ended since (done or failed), remember the rest.
    The first list from a server (and account) is only remembered, so its history is not announced. */
- (void)processJobs:(NSArray *)records silently:(BOOL)silent {
    NSMutableArray<NSDictionary *> *jobs = [NSMutableArray array];
    for (id record in records) if ([record isKindOfClass:[NSDictionary class]] && OWString(record[@"id"]).length) [jobs addObject:record];
    id stored = [self allJobStates][[self jobStatesKey]];
    NSDictionary *before = [stored isKindOfClass:[NSDictionary class]] ? stored : nil;
    BOOL firstList = before == nil;
    NSMutableDictionary<NSString *, NSString *> *states = [NSMutableDictionary dictionary];
    NSMutableArray<NSDictionary *> *ended = [NSMutableArray array];
    for (NSDictionary *job in jobs) {
        NSString *jobId = OWString(job[@"id"]);
        NSString *state = OWString(job[@"state"]) ?: @"";
        NSString *seen = [before[jobId] isKindOfClass:[NSString class]] ? before[jobId] : nil;
        states[jobId] = state;
        if (silent || firstList || !OWStateAnnounced(state) || [seen isEqualToString:state]) continue;
        // jobs of another account (the list holds only yours; an administrator's ?all=1 list would not) are not announced
        NSString *owner = OWString(job[@"owner"]);
        if (owner.length && self.user.length && ![owner isEqualToString:self.user]) continue;
        // never seen before: it ended between two polls, or while the app was closed (then only if recently)
        if (!seen && !OWEndedRecently(job)) continue;
        [ended addObject:job];
    }
    [self saveJobStates:states];
    [self announceJobs:ended];
    [self showJobs:jobs];
}

/** Sidebar list (active jobs first), Dock badge (running jobs), and App Nap held off while jobs are active. */
- (void)showJobs:(NSArray<NSDictionary *> *)jobs {
    self.jobs = jobs;
    NSMutableArray<NSDictionary *> *active = [NSMutableArray array];
    NSMutableArray<NSDictionary *> *others = [NSMutableArray array];
    NSInteger running = 0;
    for (NSDictionary *job in jobs) {
        NSString *state = OWString(job[@"state"]);
        if (OWStateRunning(state)) running += 1;
        [(OWStateActive(state) ? active : others) addObject:job];
    }
    NSApp.dockTile.badgeLabel = running > 0 ? [NSString stringWithFormat:@"%ld", (long)running] : nil;
    // a background app's timers are slowed down (App Nap): keep polling on time while something is queued or running
    if (active.count && !self.watchActivity) {
        self.watchActivity = [[NSProcessInfo processInfo] beginActivityWithOptions:NSActivityUserInitiatedAllowingIdleSystemSleep reason:@"Watching analysis jobs"];
    } else if (!active.count && self.watchActivity) {
        [[NSProcessInfo processInfo] endActivity:self.watchActivity];
        self.watchActivity = nil;
    }
    self.jobsCountLabel.stringValue = active.count ? [NSString stringWithFormat:@"%lu active", (unsigned long)active.count] : @"";

    for (NSView *view in [self.jobStack.arrangedSubviews copy]) [view removeFromSuperview];
    NSMutableArray<NSDictionary *> *listed = [active mutableCopy];
    [listed addObjectsFromArray:others];
    NSUInteger count = MIN(listed.count, OWSidebarJobs);
    for (NSUInteger i = 0; i < count; i++) {
        OWJobRow *row = [[OWJobRow alloc] initWithJob:listed[i] target:self action:@selector(openJobRow:)];
        [self.jobStack addArrangedSubview:row];
        [row.widthAnchor constraintEqualToAnchor:self.jobStack.widthAnchor].active = YES;
    }
    self.jobStack.hidden = count == 0;
    self.jobsEmptyLabel.hidden = count > 0;
    [self followPath:[(self.currentPath ?: @"") componentsSeparatedByString:@"?"].firstObject];
}

- (void)openJobRow:(OWJobRow *)sender { if (sender.jobId.length) [self loadPage:OWJobPath(sender.jobId)]; }

- (BOOL)notificationsEnabled {
    NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
    return [defaults objectForKey:OWNotifyDefaultsKey] ? [defaults boolForKey:OWNotifyDefaultsKey] : YES;
}

- (void)toggleNotifications:(id)sender {
    BOOL enabled = ![self notificationsEnabled];
    [[NSUserDefaults standardUserDefaults] setBool:enabled forKey:OWNotifyDefaultsKey];
    self.notifyMenuItem.state = enabled ? NSControlStateValueOn : NSControlStateValueOff;
    if (enabled) [self requestNotificationPermission];
}

- (void)requestNotificationPermission {
    if (!NSBundle.mainBundle.bundleIdentifier.length) return;
    UNAuthorizationOptions options = UNAuthorizationOptionAlert | UNAuthorizationOptionSound | UNAuthorizationOptionBadge;
    [[UNUserNotificationCenter currentNotificationCenter] requestAuthorizationWithOptions:options completionHandler:^(BOOL granted, NSError *error) {
        if (error || !granted) NSLog(@"notifications not allowed: %@", error.localizedDescription ?: @"declined in System Settings");
    }];
}

- (void)announceJobs:(NSArray<NSDictionary *> *)jobs {
    NSUInteger shown = 0;
    for (NSDictionary *job in jobs) {
        if (shown == 4) break;
        [self announceJob:job];
        shown += 1;
    }
    if (jobs.count > shown) {
        NSString *title = [NSString stringWithFormat:@"%lu more jobs ended", (unsigned long)(jobs.count - shown)];
        [self postNotificationWithTitle:title body:@"Open Jobs to see how they went." jobId:nil
                             identifier:[NSString stringWithFormat:@"jobs-%.0f", [NSDate date].timeIntervalSince1970]];
    }
}

- (void)announceJob:(NSDictionary *)job {
    NSString *jobId = OWString(job[@"id"]);
    NSString *state = OWString(job[@"state"]);
    BOOL failed = [state isEqualToString:@"failed"];
    NSString *server = OWJobServer(job);
    NSString *step = OWString(job[@"step"]);
    NSMutableString *body = [NSMutableString stringWithFormat:@"%@ job", OWKindLabel(OWString(job[@"kind"]))];
    if (server.length) [body appendFormat:@" on %@", server];
    [body appendString:failed ? @" failed" : @" finished"];
    if (failed && step.length) [body appendFormat:@" at %@", step];
    [body appendString:@". Click to open its log."];
    [self postNotificationWithTitle:[NSString stringWithFormat:@"%@: %@", failed ? @"Failed" : @"Finished", OWJobTitle(job)] body:body jobId:jobId
                         identifier:[NSString stringWithFormat:@"job-%@-%@", jobId, state]];
}

- (void)postNotificationWithTitle:(NSString *)title body:(NSString *)body jobId:(NSString *)jobId identifier:(NSString *)identifier {
    NSLog(@"notification: %@ — %@", title, body);
    // preview builds (CI) log them instead: the runner would only ask for permission
    if ([self snapshotDirectory].length || ![self notificationsEnabled] || !NSBundle.mainBundle.bundleIdentifier.length) return;
    UNMutableNotificationContent *content = [[UNMutableNotificationContent alloc] init];
    content.title = title;
    content.body = body;
    content.sound = [UNNotificationSound defaultSound];
    content.threadIdentifier = @"jobs";
    content.userInfo = jobId.length ? @{@"job": jobId} : @{};
    UNNotificationRequest *request = [UNNotificationRequest requestWithIdentifier:identifier content:content trigger:nil];
    [[UNUserNotificationCenter currentNotificationCenter] addNotificationRequest:request withCompletionHandler:^(NSError *error) {
        if (error) NSLog(@"notification not shown: %@", error.localizedDescription);
    }];
}

// banners also while the app is in front
- (void)userNotificationCenter:(UNUserNotificationCenter *)center willPresentNotification:(UNNotification *)notification
         withCompletionHandler:(void (^)(UNNotificationPresentationOptions options))completionHandler {
    completionHandler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList | UNNotificationPresentationOptionSound);
}

// a click on a notification: the job's log (or the job list)
- (void)userNotificationCenter:(UNUserNotificationCenter *)center didReceiveNotificationResponse:(UNNotificationResponse *)response
         withCompletionHandler:(void (^)(void))completionHandler {
    id jobId = response.notification.request.content.userInfo[@"job"];
    NSString *path = [jobId isKindOfClass:[NSString class]] && [jobId length] ? OWJobPath(jobId) : @"/workbench/jobs/";
    dispatch_async(dispatch_get_main_queue(), ^{
        [NSApp activateIgnoringOtherApps:YES];
        [self.window makeKeyAndOrderFront:nil];
        [self openPathWhenConnected:path];
    });
    completionHandler();
}

#pragma mark Appearance (System / Light / Dark), as in Omics Canvas

- (NSString *)appearancePreference {
    NSString *value = [[NSUserDefaults standardUserDefaults] stringForKey:OWAppearanceDefaultsKey];
    return [@[@"light", @"dark"] containsObject:value] ? value : @"system";
}

- (void)setAppearancePreference:(NSString *)preference {
    NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
    if ([preference isEqualToString:@"light"] || [preference isEqualToString:@"dark"]) [defaults setObject:preference forKey:OWAppearanceDefaultsKey];
    else [defaults removeObjectForKey:OWAppearanceDefaultsKey];
    [self applyAppearancePreference];
}

- (void)applyAppearancePreference {
    NSString *preference = [self appearancePreference];
    if ([preference isEqualToString:@"light"]) NSApp.appearance = [NSAppearance appearanceNamed:NSAppearanceNameAqua];
    else if ([preference isEqualToString:@"dark"]) NSApp.appearance = [NSAppearance appearanceNamed:NSAppearanceNameDarkAqua];
    else NSApp.appearance = nil;
    [self appearanceDidChange];
}

- (BOOL)systemPrefersDark {
    return [[[NSUserDefaults standardUserDefaults] stringForKey:@"AppleInterfaceStyle"] isEqualToString:@"Dark"];
}

- (void)appearanceDidChange {
    BOOL dark = GCAppearanceIsDark(NSApp.effectiveAppearance);
    self.themeButton.image = GCSymbol(dark ? @"sun.max" : @"moon", 13, @"Switch theme");
    self.themeButton.toolTip = dark ? @"Switch to light theme" : @"Switch to dark theme";
    [self.themeButton refreshColors];
    NSString *preference = [self appearancePreference];
    for (NSMenuItem *item in self.appearanceMenuItems) {
        item.state = [item.representedObject isEqualToString:preference] ? NSControlStateValueOn : NSControlStateValueOff;
    }
    for (OWSectionCard *card in self.cards) card.chosen = card.chosen;
    // the page follows the window (its own theme button is hidden in desktop mode)
    [self callDesktopMethod:@"setTheme" arguments:@[dark ? @"dark" : @"light"] completion:nil];
}

- (void)toggleAppearance:(id)sender {
    NSString *next = GCAppearanceIsDark(NSApp.effectiveAppearance) ? @"light" : @"dark";
    BOOL matchesSystem = [next isEqualToString:@"dark"] == [self systemPrefersDark];
    [self setAppearancePreference:matchesSystem ? @"system" : next];
}

- (void)chooseAppearance:(NSMenuItem *)sender { [self setAppearancePreference:sender.representedObject]; }

#pragma mark Preview snapshots (OMICS_WORKBENCH_SNAPSHOT_DIR, for CI preview builds)

- (NSString *)snapshotDirectory { return [NSProcessInfo processInfo].environment[@"OMICS_WORKBENCH_SNAPSHOT_DIR"]; }

- (void)scheduleSnapshotCaptureIfRequested {
    NSString *directory = [self snapshotDirectory];
    if (!directory.length || self.snapshotScheduled) return;
    self.snapshotScheduled = YES;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(8 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        NSDictionary *env = [NSProcessInfo processInfo].environment;
        // CI cannot reach the lab server: OMICS_WORKBENCH_SNAPSHOT_JOBS (a JSON file: "user", "admin", "currentPath",
        // "baseline" — jobs as /workbench/api/jobs lists them, seen first — and "jobs", seen next) fills the sidebar and
        // the Dock badge, and logs the notifications the change from "baseline" to "jobs" gives ("notification: …")
        NSString *fixture = env[@"OMICS_WORKBENCH_SNAPSHOT_JOBS"];
        NSData *data = fixture.length ? [NSData dataWithContentsOfFile:fixture] : nil;
        NSDictionary *sample = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if ([sample isKindOfClass:[NSDictionary class]] && [sample[@"jobs"] isKindOfClass:[NSArray class]]) {
            self.fixtureLoaded = YES;
            [self applyIdentity:sample status:200];
            self.currentPath = [sample[@"currentPath"] isKindOfClass:[NSString class]] ? sample[@"currentPath"] : @"";
            [self processJobs:[sample[@"baseline"] isKindOfClass:[NSArray class]] ? sample[@"baseline"] : @[] silently:YES];
            [self processJobs:sample[@"jobs"] silently:NO];
        }
        // documentation screenshots against a test server: OMICS_WORKBENCH_SNAPSHOT_PAGES ("name=/path;name=/path"):
        // each page is captured as <name>.png
        NSString *pages = env[@"OMICS_WORKBENCH_SNAPSHOT_PAGES"];
        if (pages.length) {
            NSLog(@"snapshot screen %@", NSStringFromRect(NSScreen.mainScreen.frame));
            [self.window setFrame:NSMakeRect(0, 0, 1400, 900) display:YES];
            [self capturePages:[pages componentsSeparatedByString:@";"] directory:directory];
            return;
        }
        [self captureThemes:@[@"light", @"dark"] directory:directory];
    });
}

- (void)capturePages:(NSArray<NSString *> *)pages directory:(NSString *)directory {
    if (!pages.count) { [NSApp terminate:nil]; return; }
    NSArray<NSString *> *parts = [pages.firstObject componentsSeparatedByString:@"="];
    NSArray<NSString *> *rest = [pages subarrayWithRange:NSMakeRange(1, pages.count - 1)];
    if (parts.count < 2) { [self capturePages:rest directory:directory]; return; }
    NSString *name = parts.firstObject, *path = [[parts subarrayWithRange:NSMakeRange(1, parts.count - 1)] componentsJoinedByString:@"="];
    NSString *theme = [name hasSuffix:@"-dark"] ? @"dark" : @"light";
    [self setAppearancePreference:theme];
    [self loadPage:path];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(14 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [self pollJobs];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            [self captureWindowToPath:[directory stringByAppendingPathComponent:[name stringByAppendingString:@".png"]] completion:^{
                [self capturePages:rest directory:directory];
            }];
        });
    });
}

- (void)captureThemes:(NSArray<NSString *> *)themes directory:(NSString *)directory {
    if (!themes.count) { [NSApp terminate:nil]; return; }
    [self setAppearancePreference:themes.firstObject];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(3 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        NSString *path = [directory stringByAppendingPathComponent:[NSString stringWithFormat:@"window-%@.png", themes.firstObject]];
        // the page's own state, so CI can check that the page followed the window ("page theme: <window> <page> <bg>")
        NSString *probe = @"(() => { const d = document.documentElement; return [location.pathname, d.dataset.theme || '-', !!window.VizHubDesktop, getComputedStyle(document.body).backgroundColor].join(' '); })()";
        [self.webView evaluateJavaScript:probe completionHandler:^(id result, NSError *error) {
            NSLog(@"page theme: %@ %@", themes.firstObject, [result isKindOfClass:[NSString class]] ? result : (error.localizedDescription ?: @"?"));
            [self captureWindowToPath:path completion:^{
                [self captureThemes:[themes subarrayWithRange:NSMakeRange(1, themes.count - 1)] directory:directory];
            }];
        }];
    });
}

- (void)captureWindowToPath:(NSString *)path completion:(void (^)(void))completion {
    NSView *root = self.window.contentView;
    NSRect bounds = root.bounds;
    NSBitmapImageRep *chrome = [root bitmapImageRepForCachingDisplayInRect:bounds];
    [root cacheDisplayInRect:bounds toBitmapImageRep:chrome];
    NSRect webRect = [self.webView convertRect:self.webView.bounds toView:nil];
    [self.webView takeSnapshotWithConfiguration:nil completionHandler:^(NSImage *webImage, NSError *error) {
        NSImage *image = [NSImage imageWithSize:bounds.size flipped:NO drawingHandler:^BOOL(NSRect rect) {
            [chrome drawInRect:rect];
            if (webImage) [webImage drawInRect:webRect];
            return YES;
        }];
        NSRect proposed = NSMakeRect(0, 0, bounds.size.width, bounds.size.height);
        CGImageRef cgImage = [image CGImageForProposedRect:&proposed context:nil hints:nil];
        if (cgImage) {
            NSBitmapImageRep *output = [[NSBitmapImageRep alloc] initWithCGImage:cgImage];
            [[output representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:path atomically:YES];
        }
        completion();
    }];
}

#pragma mark Lifecycle and menu

// The app keeps running when its window is closed, so job notifications keep coming; the Dock icon (or
// Window → Omics Workbench, ⌘0) brings the window back.
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender { return NO; }

- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)flag {
    if (!flag) [self.window makeKeyAndOrderFront:nil];
    return YES;
}

- (void)showMainWindow:(id)sender { [self.window makeKeyAndOrderFront:nil]; }

- (void)applicationWillTerminate:(NSNotification *)notification {
    [self.pollTimer invalidate];
    if (self.watchActivity) [[NSProcessInfo processInfo] endActivity:self.watchActivity];
    NSApp.dockTile.badgeLabel = nil;
    [self.connection close];
    [self.webView.configuration.userContentController removeScriptMessageHandlerForName:@"vizhub"];
}

- (void)dealloc {
    @try { [self.webView removeObserver:self forKeyPath:@"estimatedProgress"]; } @catch (__unused NSException *exception) {}
    @try { [self.webView removeObserver:self forKeyPath:@"loading"]; } @catch (__unused NSException *exception) {}
    @try { [self.webView removeObserver:self forKeyPath:@"canGoBack"]; } @catch (__unused NSException *exception) {}
    @try { [NSApp removeObserver:self forKeyPath:@"effectiveAppearance"]; } @catch (__unused NSException *exception) {}
}

- (NSMenuItem *)menuItem:(NSString *)title action:(SEL)action key:(NSString *)key modifiers:(NSEventModifierFlags)modifiers {
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:title action:action keyEquivalent:key];
    item.target = self;
    if (modifiers) item.keyEquivalentModifierMask = modifiers;
    return item;
}

- (void)buildApplicationMenu {
    NSMenu *mainMenu = [[NSMenu alloc] initWithTitle:@""];

    NSMenuItem *applicationItem = [[NSMenuItem alloc] initWithTitle:@"" action:nil keyEquivalent:@""];
    NSMenu *applicationMenu = [[NSMenu alloc] initWithTitle:@"Omics Workbench"];
    [applicationMenu addItemWithTitle:@"About Omics Workbench" action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    [applicationMenu addItem:[NSMenuItem separatorItem]];
    [applicationMenu addItem:[self menuItem:@"Connect to Server…" action:@selector(editServerAddress:) key:@"," modifiers:0]];
    [applicationMenu addItem:[self menuItem:@"Sign Out" action:@selector(signOut:) key:@"" modifiers:0]];
    [applicationMenu addItem:[NSMenuItem separatorItem]];
    [applicationMenu addItemWithTitle:@"Hide Omics Workbench" action:@selector(hide:) keyEquivalent:@"h"];
    [applicationMenu addItemWithTitle:@"Quit Omics Workbench" action:@selector(terminate:) keyEquivalent:@"q"];
    applicationItem.submenu = applicationMenu;
    [mainMenu addItem:applicationItem];

    NSMenuItem *fileItem = [[NSMenuItem alloc] initWithTitle:@"" action:nil keyEquivalent:@""];
    NSMenu *fileMenu = [[NSMenu alloc] initWithTitle:@"File"];
    [fileMenu addItem:[self menuItem:@"Check Jobs Now" action:@selector(checkJobsNow:) key:@"j" modifiers:NSEventModifierFlagCommand | NSEventModifierFlagShift]];
    self.notifyMenuItem = [self menuItem:@"Notify When Jobs End" action:@selector(toggleNotifications:) key:@"" modifiers:0];
    self.notifyMenuItem.state = [self notificationsEnabled] ? NSControlStateValueOn : NSControlStateValueOff;
    [fileMenu addItem:self.notifyMenuItem];
    [fileMenu addItem:[NSMenuItem separatorItem]];
    [fileMenu addItem:[self menuItem:@"Open in Browser" action:@selector(openInBrowser:) key:@"b" modifiers:NSEventModifierFlagCommand | NSEventModifierFlagShift]];
    [fileMenu addItem:[NSMenuItem separatorItem]];
    [fileMenu addItemWithTitle:@"Close Window" action:@selector(performClose:) keyEquivalent:@"w"];
    fileItem.submenu = fileMenu;
    [mainMenu addItem:fileItem];

    NSMenuItem *editItem = [[NSMenuItem alloc] initWithTitle:@"" action:nil keyEquivalent:@""];
    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"Edit"];
    [editMenu addItemWithTitle:@"Undo" action:@selector(undo:) keyEquivalent:@"z"];
    [editMenu addItemWithTitle:@"Redo" action:@selector(redo:) keyEquivalent:@"Z"];
    [editMenu addItem:[NSMenuItem separatorItem]];
    [editMenu addItemWithTitle:@"Cut" action:@selector(cut:) keyEquivalent:@"x"];
    [editMenu addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
    [editMenu addItemWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"];
    [editMenu addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
    editItem.submenu = editMenu;
    [mainMenu addItem:editItem];

    NSMenuItem *viewItem = [[NSMenuItem alloc] initWithTitle:@"" action:nil keyEquivalent:@""];
    NSMenu *viewMenu = [[NSMenu alloc] initWithTitle:@"View"];
    NSMutableArray<NSMenuItem *> *sectionItems = [NSMutableArray array];
    NSInteger number = 1;
    for (NSDictionary<NSString *, NSString *> *section in OWSections()) {
        NSMenuItem *item = [self menuItem:section[@"menu"] action:@selector(chooseSectionFromMenu:) key:[NSString stringWithFormat:@"%ld", (long)number++] modifiers:0];
        item.representedObject = section[@"key"];
        if ([section[@"key"] isEqualToString:@"servers"]) { item.hidden = YES; self.serversMenuItem = item; }
        [viewMenu addItem:item];
        [sectionItems addObject:item];
    }
    self.sectionMenuItems = sectionItems;
    [viewMenu addItem:[NSMenuItem separatorItem]];
    [viewMenu addItem:[self menuItem:@"Back" action:@selector(goBack:) key:@"[" modifiers:0]];
    [viewMenu addItem:[self menuItem:@"Reload" action:@selector(reloadOrStop:) key:@"r" modifiers:0]];
    [viewMenu addItem:[NSMenuItem separatorItem]];
    NSMenuItem *appearanceItem = [[NSMenuItem alloc] initWithTitle:@"Appearance" action:nil keyEquivalent:@""];
    NSMenu *appearanceMenu = [[NSMenu alloc] initWithTitle:@"Appearance"];
    NSMutableArray<NSMenuItem *> *appearanceItems = [NSMutableArray array];
    for (NSArray<NSString *> *entry in @[@[@"Use System Setting", @"system"], @[@"Light", @"light"], @[@"Dark", @"dark"]]) {
        NSMenuItem *item = [self menuItem:entry[0] action:@selector(chooseAppearance:) key:@"" modifiers:0];
        item.representedObject = entry[1];
        [appearanceMenu addItem:item];
        [appearanceItems addObject:item];
    }
    [appearanceMenu addItem:[NSMenuItem separatorItem]];
    [appearanceMenu addItem:[self menuItem:@"Toggle Light/Dark" action:@selector(toggleAppearance:) key:@"t" modifiers:NSEventModifierFlagCommand | NSEventModifierFlagShift]];
    self.appearanceMenuItems = appearanceItems;
    appearanceItem.submenu = appearanceMenu;
    [viewMenu addItem:appearanceItem];
    viewItem.submenu = viewMenu;
    [mainMenu addItem:viewItem];

    NSMenuItem *windowItem = [[NSMenuItem alloc] initWithTitle:@"" action:nil keyEquivalent:@""];
    NSMenu *windowMenu = [[NSMenu alloc] initWithTitle:@"Window"];
    [windowMenu addItemWithTitle:@"Minimize" action:@selector(performMiniaturize:) keyEquivalent:@"m"];
    [windowMenu addItemWithTitle:@"Zoom" action:@selector(performZoom:) keyEquivalent:@""];
    [windowMenu addItem:[NSMenuItem separatorItem]];
    [windowMenu addItem:[self menuItem:@"Omics Workbench" action:@selector(showMainWindow:) key:@"0" modifiers:0]];
    windowItem.submenu = windowMenu;
    [mainMenu addItem:windowItem];
    NSApp.windowsMenu = windowMenu;

    [NSApp setMainMenu:mainMenu];
}

#pragma mark Loading state

- (void)reloadOrStop:(id)sender {
    if (self.webView.loading) { [self.webView stopLoading]; [self setStatus:@"Stopped" state:@"ready"]; }
    else if (self.webView.URL && [self pageIsOnServer]) { [self setStatus:@"Loading" state:@"busy"]; [self.webView reloadFromOrigin]; }
    else if (self.connected) [self showSection:self.sectionKey];
    else [self connectAndShowSection];
}

- (void)observeValueForKeyPath:(NSString *)keyPath ofObject:(id)object change:(NSDictionary<NSKeyValueChangeKey, id> *)change context:(void *)context {
    if ([keyPath isEqualToString:@"estimatedProgress"] || [keyPath isEqualToString:@"loading"]) {
        CGFloat width = self.progressBar.superview.bounds.size.width;
        self.progressWidthConstraint.constant = width * self.webView.estimatedProgress;
        self.progressBar.hidden = !self.webView.loading;
        self.reloadButton.image = GCSymbol(self.webView.loading ? @"xmark" : @"arrow.clockwise", 13, @"Reload or stop");
        self.reloadButton.toolTip = self.webView.loading ? @"Stop loading" : @"Reload";
        [self.reloadButton refreshColors];
        return;
    }
    if ([keyPath isEqualToString:@"canGoBack"]) { self.backButton.enabled = self.webView.canGoBack; return; }
    if ([keyPath isEqualToString:@"effectiveAppearance"]) {
        dispatch_async(dispatch_get_main_queue(), ^{ [self appearanceDidChange]; });
        return;
    }
    [super observeValueForKeyPath:keyPath ofObject:object change:change context:context];
}

#pragma mark Navigation, downloads and dialogs (as in Omics Canvas)

- (void)webView:(WKWebView *)webView didStartProvisionalNavigation:(WKNavigation *)navigation {
    if (self.showingErrorPage) { self.showingErrorPage = NO; return; }  // the local "not reachable" page keeps the status
    [self setStatus:@"Loading" state:@"busy"];
    self.progressBar.hidden = NO;
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    self.progressBar.hidden = YES;
    if (![webView.URL.scheme isEqualToString:@"http"] && ![webView.URL.scheme isEqualToString:@"https"]) return;  // the local "not reachable" page
    NSString *title = webView.title ?: @"";
    for (NSString *suffix in @[@" · Omics Workbench", @" · Omics Studio", @" · Omics Canvas"]) {
        NSRange range = [title rangeOfString:suffix];
        if (range.location != NSNotFound) title = [title substringToIndex:range.location];
    }
    self.pageTitleLabel.stringValue = title;
    self.window.title = title.length ? title : @"Omics Workbench";
    // pages that do not post a "page" message (the sign-in and admin pages) still select their card
    // (NSURL.path drops the trailing slash; the components keep it)
    NSURLComponents *components = [NSURLComponents componentsWithURL:webView.URL resolvingAgainstBaseURL:NO];
    NSString *path = components.percentEncodedPath ?: @"";
    self.currentPath = components.percentEncodedQuery.length ? [NSString stringWithFormat:@"%@?%@", path, components.percentEncodedQuery] : path;
    [self followPath:path];
    [self setStatus:@"Connected" state:@"ready"];
    [self appearanceDidChange];
    [self pollJobsSoon];
    [self scheduleSnapshotCaptureIfRequested];
}

- (void)webView:(WKWebView *)webView didFailNavigation:(WKNavigation *)navigation withError:(NSError *)error { [self showNavigationError:error]; }
- (void)webView:(WKWebView *)webView didFailProvisionalNavigation:(WKNavigation *)navigation withError:(NSError *)error { [self showNavigationError:error]; }
- (void)showNavigationError:(NSError *)error {
    if (error.code == NSURLErrorCancelled) return;
    [self setStatus:@"Server unavailable" state:@"error"];
    self.statusLabel.toolTip = [error.localizedDescription stringByAppendingString:@" — from outside Stanford, connect the VPN or use SSH port forwarding (Server…)."];
    self.progressBar.hidden = YES;
    // a themed explanation instead of an empty white page; its colors follow the window (prefers-color-scheme)
    NSString *retry = [self pageURL:OWSectionForKey(self.sectionKey)[@"page"]].absoluteString ?: @"";
    NSString *server = [self serverRoot].absoluteString ?: @"";
    NSString *html = [NSString stringWithFormat:
        @"<!doctype html><meta charset=utf-8><style>:root{color-scheme:light dark;--bg:#e5e9f0;--text:#2e3440;--muted:#69728a;--accent:#4c7a86}"
        "@media (prefers-color-scheme:dark){:root{--bg:#242933;--text:#eceff4;--muted:#9aa5b8;--accent:#88c0d0}}"
        "html,body{margin:0;height:100%%;background:var(--bg);color:var(--text);font:14px -apple-system,sans-serif}"
        "main{height:100%%;display:grid;place-content:center;text-align:center;gap:6px;padding:24px}h1{font:600 19px Georgia,serif;margin:0 0 4px}"
        "p{margin:0;color:var(--muted);max-width:440px}code{font:12px ui-monospace,monospace}a{color:var(--accent);font-weight:600}</style>"
        "<main><h1>The Omics Studio server is not reachable</h1><p>Could not connect to <code>%@</code>.</p>"
        "<p>From outside Stanford, connect the VPN first, or set up SSH port forwarding under <b>Server…</b> in the sidebar (also for another address).</p>"
        "<p style='margin-top:10px'><a href='%@'>Try again</a></p></main>",
        server, retry];
    self.showingErrorPage = YES;
    [self.webView loadHTMLString:html baseURL:nil];
    [self scheduleSnapshotCaptureIfRequested];  // preview builds (CI) cannot reach the lab server: capture the window anyway
}
- (void)webViewWebContentProcessDidTerminate:(WKWebView *)webView { [self setStatus:@"Page process stopped" state:@"error"]; }

/** Pages that stay in this window: the workbench, the admin page (Servers), the sign-in (auth/) and the APIs. */
- (BOOL)isWorkbenchURL:(NSURL *)url {
    if (![self isSameServer:url]) return NO;
    NSString *path = url.path ?: @"";
    // the sign-in page (auth/) stays in the window: a server that asks for sign-in shows it first, then the page
    return [path isEqualToString:@"/workbench"] || [path hasPrefix:@"/workbench/"] || [path isEqualToString:@"/admin"] || [path hasPrefix:@"/admin/"]
        || [path hasPrefix:@"/auth/"] || [path hasPrefix:@"/hub/api/"] || [path hasPrefix:@"/hub/static/"] || [path hasPrefix:@"/genome-canvas/api/"];
}

// Links leaving the workbench. Genome Canvas pages on this server go to Genome Canvas.app when it is installed
// (genomecanvas://open?locus=…&highlight=…, opened on the server that app uses); otherwise, and for the viewers
// (Omics Canvas), the home page and other sites, the default browser.
- (void)openOutside:(NSURL *)url {
    if (!url) return;
    BOOL sameServer = [self isSameServer:url];
    // NSURL.path drops the trailing slash ("/genome-canvas/?locus=…" -> "/genome-canvas"); the components keep it
    NSString *path = [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:YES].path ?: @"";
    BOOL canvasPage = [path isEqualToString:@"/genome-canvas"] || ([path hasPrefix:@"/genome-canvas/"] && ![path hasPrefix:@"/genome-canvas/api/"]);
    if (sameServer && canvasPage) {
        NSURLComponents *link = [NSURLComponents componentsWithString:@"genomecanvas://open"];
        NSMutableArray<NSURLQueryItem *> *items = [NSMutableArray array];
        for (NSURLQueryItem *item in [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO].queryItems ?: @[]) {
            if (![@[@"locus", @"genome", @"highlight", @"color", @"track"] containsObject:item.name]) continue;
            // track=<name>|/server/path: make the path absolute, Genome Canvas.app may be on another server or its local backend
            NSRange bar = [item.value rangeOfString:@"|" options:NSBackwardsSearch];
            if ([item.name isEqualToString:@"track"] && bar.location != NSNotFound && bar.location + 1 < item.value.length && [item.value characterAtIndex:bar.location + 1] == '/') {
                NSURL *absolute = [NSURL URLWithString:[item.value substringFromIndex:bar.location + 1] relativeToURL:url];
                [items addObject:[NSURLQueryItem queryItemWithName:@"track" value:[NSString stringWithFormat:@"%@|%@", [item.value substringToIndex:bar.location], absolute.absoluteString]]];
                continue;
            }
            [items addObject:item];
        }
        link.queryItems = items.count ? items : nil;
        NSURL *app = [self genomeCanvasApp];
        if (link.URL && app) {
            // sent to that app directly: Launch Services may not have registered the scheme of a freshly copied app yet
            NSWorkspaceOpenConfiguration *configuration = [NSWorkspaceOpenConfiguration configuration];
            configuration.activates = YES;
            [[NSWorkspace sharedWorkspace] openURLs:@[link.URL] withApplicationAtURL:app configuration:configuration
                                  completionHandler:^(NSRunningApplication *running, NSError *error) {
                if (!error) return;
                dispatch_async(dispatch_get_main_queue(), ^{ [[NSWorkspace sharedWorkspace] openURL:url]; });
            }];
            return;
        }
        if ([[NSWorkspace sharedWorkspace] URLForApplicationWithBundleIdentifier:OWGenomeCanvasBundleID]) {
            [self setStatus:@"Update Genome Canvas.app to 1.7 to open links in it" state:@"ready"];
        }
    }
    [[NSWorkspace sharedWorkspace] openURL:url];
}

// Genome Canvas.app (1.7 or later: it declares genomecanvas:// links), or nil
- (NSURL *)genomeCanvasApp {
    NSWorkspace *workspace = [NSWorkspace sharedWorkspace];
    NSMutableArray<NSURL *> *candidates = [NSMutableArray array];
    NSURL *handler = [workspace URLForApplicationToOpenURL:[NSURL URLWithString:@"genomecanvas://open"]];
    if (handler) [candidates addObject:handler];
    NSURL *byId = [workspace URLForApplicationWithBundleIdentifier:OWGenomeCanvasBundleID];
    if (byId) [candidates addObject:byId];
    for (NSString *dir in @[@"/Applications", [NSHomeDirectory() stringByAppendingPathComponent:@"Applications"]]) {
        [candidates addObject:[NSURL fileURLWithPath:[dir stringByAppendingPathComponent:@"Genome Canvas.app"]]];
    }
    for (NSURL *candidate in candidates) {
        NSDictionary *info = [NSBundle bundleWithURL:candidate].infoDictionary;
        for (NSDictionary *type in info[@"CFBundleURLTypes"] ?: @[]) {
            if ([type[@"CFBundleURLSchemes"] containsObject:@"genomecanvas"]) return candidate;
        }
    }
    return nil;
}

- (void)webView:(WKWebView *)webView decidePolicyForNavigationAction:(WKNavigationAction *)navigationAction decisionHandler:(void (^)(WKNavigationActionPolicy))decisionHandler {
    if (navigationAction.shouldPerformDownload) { decisionHandler(WKNavigationActionPolicyDownload); return; }
    NSURL *url = navigationAction.request.URL;
    NSString *scheme = url.scheme.lowercaseString;
    if ([@[@"blob", @"about", @"data"] containsObject:scheme ?: @""]) { decisionHandler(WKNavigationActionPolicyAllow); return; }
    // this app shows the workbench pages; everything else leaves through openOutside:
    if (navigationAction.targetFrame.isMainFrame && ![self isWorkbenchURL:url]) {
        [self openOutside:url];
        decisionHandler(WKNavigationActionPolicyCancel);
        return;
    }
    decisionHandler(WKNavigationActionPolicyAllow);
}

- (void)webView:(WKWebView *)webView decidePolicyForNavigationResponse:(WKNavigationResponse *)navigationResponse decisionHandler:(void (^)(WKNavigationResponsePolicy))decisionHandler {
    NSString *disposition = @"";
    if ([navigationResponse.response isKindOfClass:[NSHTTPURLResponse class]]) disposition = [[(NSHTTPURLResponse *)navigationResponse.response valueForHTTPHeaderField:@"Content-Disposition"] lowercaseString] ?: @"";
    decisionHandler(!navigationResponse.canShowMIMEType || [disposition containsString:@"attachment"] ? WKNavigationResponsePolicyDownload : WKNavigationResponsePolicyAllow);
}
- (void)webView:(WKWebView *)webView navigationAction:(WKNavigationAction *)navigationAction didBecomeDownload:(WKDownload *)download { download.delegate = self; }
- (void)webView:(WKWebView *)webView navigationResponse:(WKNavigationResponse *)navigationResponse didBecomeDownload:(WKDownload *)download { download.delegate = self; }

- (void)download:(WKDownload *)download decideDestinationUsingResponse:(NSURLResponse *)response suggestedFilename:(NSString *)suggestedFilename completionHandler:(void (^)(NSURL * _Nullable destination))completionHandler {
    NSSavePanel *panel = [NSSavePanel savePanel];
    panel.nameFieldStringValue = suggestedFilename;
    panel.canCreateDirectories = YES;
    panel.title = @"Save File";
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result) {
        if (result != NSModalResponseOK || !panel.URL) { completionHandler(nil); return; }
        NSURL *destination = panel.URL;
        if ([[NSFileManager defaultManager] fileExistsAtPath:destination.path]) [[NSFileManager defaultManager] removeItemAtURL:destination error:nil];
        [self.downloadDestinations setObject:destination forKey:download];
        [self setStatus:@"Saving" state:@"busy"];
        completionHandler(destination);
    }];
}
- (void)downloadDidFinish:(WKDownload *)download {
    NSURL *destination = [self.downloadDestinations objectForKey:download];
    [self.downloadDestinations removeObjectForKey:download];
    [self setStatus:(destination.lastPathComponent.length ? [@"Saved " stringByAppendingString:destination.lastPathComponent] : @"Saved") state:@"ready"];
}
- (void)download:(WKDownload *)download didFailWithError:(NSError *)error resumeData:(NSData *)resumeData {
    [self.downloadDestinations removeObjectForKey:download];
    [self setStatus:@"Saving failed" state:@"error"];
    self.statusLabel.toolTip = error.localizedDescription;
}

- (void)webView:(WKWebView *)webView runOpenPanelWithParameters:(WKOpenPanelParameters *)parameters initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(NSArray<NSURL *> * _Nullable URLs))completionHandler {
    NSOpenPanel *panel = [NSOpenPanel openPanel];
    panel.allowsMultipleSelection = parameters.allowsMultipleSelection;
    panel.canChooseDirectories = parameters.allowsDirectories;
    panel.canChooseFiles = YES;
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result) { completionHandler(result == NSModalResponseOK ? panel.URLs : nil); }];
}
- (WKWebView *)webView:(WKWebView *)webView createWebViewWithConfiguration:(WKWebViewConfiguration *)configuration forNavigationAction:(WKNavigationAction *)navigationAction windowFeatures:(WKWindowFeatures *)windowFeatures {
    // target=_blank links: workbench pages stay in this window, everything else leaves through openOutside:
    NSURL *url = navigationAction.request.URL;
    if ([self isWorkbenchURL:url]) [webView loadRequest:navigationAction.request];
    else [self openOutside:url];
    return nil;
}
- (void)webView:(WKWebView *)webView runJavaScriptAlertPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(void))completionHandler {
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"Omics Workbench";
    alert.informativeText = message;
    [alert addButtonWithTitle:@"OK"];
    [alert beginSheetModalForWindow:self.window completionHandler:^(__unused NSModalResponse result) { completionHandler(); }];
}
- (void)webView:(WKWebView *)webView runJavaScriptConfirmPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(BOOL result))completionHandler {
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"Omics Workbench";
    alert.informativeText = message;
    [alert addButtonWithTitle:@"OK"];
    [alert addButtonWithTitle:@"Cancel"];
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result) { completionHandler(result == NSAlertFirstButtonReturn); }];
}
- (void)webView:(WKWebView *)webView runJavaScriptTextInputPanelWithPrompt:(NSString *)prompt defaultText:(NSString *)defaultText initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(NSString * _Nullable result))completionHandler {
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"Omics Workbench";
    alert.informativeText = prompt;
    [alert addButtonWithTitle:@"OK"];
    [alert addButtonWithTitle:@"Cancel"];
    NSTextField *input = [NSTextField textFieldWithString:defaultText ?: @""];
    input.frame = NSMakeRect(0, 0, 320, 24);
    alert.accessoryView = input;
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result) { completionHandler(result == NSAlertFirstButtonReturn ? input.stringValue : nil); }];
}

@end

int main(int argc, const char *argv[]) {
    OCRunAskpassIfRequested();  // ssh asking this app for the SSH password (port forwarding)
    @autoreleasepool {
        NSApplication *application = [NSApplication sharedApplication];
        application.activationPolicy = NSApplicationActivationPolicyRegular;
        OWAppDelegate *delegate = [[OWAppDelegate alloc] init];
        application.delegate = delegate;
        [application run];
    }
    return 0;
}
