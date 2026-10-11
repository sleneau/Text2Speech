#import <Foundation/Foundation.h>
#import <Vision/Vision.h>
#import <ImageIO/ImageIO.h>

static void fail(NSString *message) {
    fprintf(stderr, "%s\n", message.UTF8String);
    exit(1);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 3) {
            fail(@"usage: vision_ocr IMAGE LANGUAGE");
        }

        NSString *imagePath = [NSString stringWithUTF8String:argv[1]];
        NSString *language = [NSString stringWithUTF8String:argv[2]];
        NSURL *imageURL = [NSURL fileURLWithPath:imagePath];
        CGImageSourceRef imageSource = CGImageSourceCreateWithURL((__bridge CFURLRef)imageURL, NULL);
        if (imageSource == NULL) {
            fail(@"ImageIO could not open the source image");
        }
        NSDictionary *properties = CFBridgingRelease(
            CGImageSourceCopyPropertiesAtIndex(imageSource, 0, NULL)
        );
        NSNumber *orientationNumber = properties[(NSString *)kCGImagePropertyOrientation];
        CGImagePropertyOrientation orientation = orientationNumber == nil
            ? kCGImagePropertyOrientationUp
            : (CGImagePropertyOrientation)orientationNumber.unsignedIntValue;
        CGImageRef image = CGImageSourceCreateImageAtIndex(imageSource, 0, NULL);
        CFRelease(imageSource);
        if (image == NULL) {
            fail(@"ImageIO could not decode the source image");
        }

        VNRecognizeTextRequest *request = [[VNRecognizeTextRequest alloc] init];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.usesLanguageCorrection = YES;
        request.recognitionLanguages = @[language];

        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc]
            initWithCGImage:image orientation:orientation options:@{}];
        NSError *visionError = nil;
        if (![handler performRequests:@[request] error:&visionError]) {
            CGImageRelease(image);
            fail([NSString stringWithFormat:@"Vision OCR error: %@", visionError.localizedDescription]);
        }
        CGImageRelease(image);

        NSArray<VNRecognizedTextObservation *> *observations = request.results ?: @[];
        observations = [observations sortedArrayUsingComparator:^NSComparisonResult(
            VNRecognizedTextObservation *left, VNRecognizedTextObservation *right
        ) {
            CGRect l = left.boundingBox;
            CGRect r = right.boundingBox;
            double leftMaxY = l.origin.y + l.size.height;
            double rightMaxY = r.origin.y + r.size.height;
            if (fabs(leftMaxY - rightMaxY) > 0.015) {
                return leftMaxY > rightMaxY ? NSOrderedAscending : NSOrderedDescending;
            }
            if (l.origin.x < r.origin.x) return NSOrderedAscending;
            if (l.origin.x > r.origin.x) return NSOrderedDescending;
            return NSOrderedSame;
        }];

        NSMutableArray *lines = [NSMutableArray array];
        for (VNRecognizedTextObservation *observation in observations) {
            VNRecognizedText *candidate = [[observation topCandidates:1] firstObject];
            if (candidate == nil) continue;
            CGRect box = observation.boundingBox;
            [lines addObject:@{
                @"text": candidate.string,
                @"confidence": @(candidate.confidence),
                @"boundingBox": @[@(box.origin.x), @(box.origin.y), @(box.size.width), @(box.size.height)]
            }];
        }

        NSError *jsonError = nil;
        NSData *json = [NSJSONSerialization dataWithJSONObject:lines options:NSJSONWritingSortedKeys error:&jsonError];
        if (json == nil) {
            fail([NSString stringWithFormat:@"JSON error: %@", jsonError.localizedDescription]);
        }
        [[NSFileHandle fileHandleWithStandardOutput] writeData:json];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
    }
    return 0;
}
