#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <PDFKit/PDFKit.h>

static void fail(NSString *message) {
    fprintf(stderr, "%s\n", message.UTF8String);
    exit(1);
}

static void printJSON(NSDictionary *value) {
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingSortedKeys error:&error];
    if (data == nil) {
        fail([NSString stringWithFormat:@"Could not encode JSON: %@", error.localizedDescription]);
    }
    [[NSFileHandle fileHandleWithStandardOutput] writeData:data];
    [[NSFileHandle fileHandleWithStandardOutput] writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
}

static PDFDocument *loadDocument(NSString *path) {
    PDFDocument *document = [[PDFDocument alloc] initWithURL:[NSURL fileURLWithPath:path]];
    if (document == nil) {
        fail(@"PDFKit could not open the PDF");
    }
    if (document.isEncrypted || document.isLocked) {
        fail(@"The PDF is encrypted or password-protected; encrypted PDFs are not supported");
    }
    if (document.pageCount < 1) {
        fail(@"The PDF contains no pages");
    }
    return document;
}

static NSInteger parsePageIndex(const char *value, PDFDocument *document) {
    NSString *text = [NSString stringWithUTF8String:value];
    NSScanner *scanner = [NSScanner scannerWithString:text];
    NSInteger index = -1;
    if (![scanner scanInteger:&index] || !scanner.isAtEnd || index < 0 ||
        index >= (NSInteger)document.pageCount) {
        fail(@"PDF page index is out of range");
    }
    return index;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 3) {
            fail(@"usage: pdf_helper info|text|render PDF [PAGE_INDEX] [OUTPUT] [DPI]");
        }
        NSString *action = [NSString stringWithUTF8String:argv[1]];
        NSString *path = [NSString stringWithUTF8String:argv[2]];
        PDFDocument *document = loadDocument(path);

        if ([action isEqualToString:@"info"]) {
            printJSON(@{
                @"page_count": @(document.pageCount),
                @"encrypted": @(document.isEncrypted),
            });
            return 0;
        }

        if (argc < 4) {
            fail(@"A zero-based page index is required");
        }
        NSInteger pageIndex = parsePageIndex(argv[3], document);
        PDFPage *page = [document pageAtIndex:pageIndex];
        if (page == nil) {
            fail(@"PDFKit could not load the requested page");
        }

        if ([action isEqualToString:@"text"]) {
            printJSON(@{
                @"page": @(pageIndex + 1),
                @"text": page.string ?: @"",
            });
            return 0;
        }

        if ([action isEqualToString:@"render"]) {
            if (argc < 6) {
                fail(@"render requires OUTPUT and DPI arguments");
            }
            NSString *output = [NSString stringWithUTF8String:argv[4]];
            NSInteger dpi = [[NSString stringWithUTF8String:argv[5]] integerValue];
            if (dpi < 72 || dpi > 600) {
                fail(@"PDF render DPI must be between 72 and 600");
            }

            NSRect bounds = [page boundsForBox:kPDFDisplayBoxCropBox];
            CGFloat scale = (CGFloat)dpi / 72.0;
            CGFloat width = MAX(1.0, ceil(NSWidth(bounds) * scale));
            CGFloat height = MAX(1.0, ceil(NSHeight(bounds) * scale));
            CGFloat longest = MAX(width, height);
            if (longest > 10000.0) {
                CGFloat safetyScale = 10000.0 / longest;
                width = floor(width * safetyScale);
                height = floor(height * safetyScale);
            }

            NSImage *image = [page thumbnailOfSize:NSMakeSize(width, height)
                                             forBox:kPDFDisplayBoxCropBox];
            CGImageRef cgImage = [image CGImageForProposedRect:NULL context:nil hints:nil];
            if (cgImage == NULL) {
                fail(@"PDFKit could not render the requested page");
            }
            NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc] initWithCGImage:cgImage];
            NSData *png = [bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
            if (png == nil || ![png writeToFile:output options:NSDataWritingAtomic error:nil]) {
                fail(@"Could not write the rendered PDF page");
            }
            printJSON(@{
                @"page": @(pageIndex + 1),
                @"width": @(bitmap.pixelsWide),
                @"height": @(bitmap.pixelsHigh),
            });
            return 0;
        }

        fail([NSString stringWithFormat:@"Unknown PDF action: %@", action]);
    }
}

