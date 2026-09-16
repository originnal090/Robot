#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

FOUNDATION_EXPORT void IPMJConfigureFileLogging(NSString *path);
FOUNDATION_EXPORT void IPMJLogMessage(NSString *message);

@interface IPMJCameraProducer : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate>

@property(nonatomic, strong, readonly, nullable) AVCaptureSession *captureSession;
@property(nonatomic, readonly) AVCaptureVideoOrientation videoOrientation;
@property(nonatomic, readonly, getter=isDatasetRecording) BOOL datasetRecording;
@property(nonatomic, readonly) NSUInteger datasetSavedCount;
@property(nonatomic, copy, readonly, nullable) NSString *datasetDirectory;

- (instancetype)initWithSocketPath:(NSString *)socketPath
                              width:(NSInteger)width
                             height:(NSInteger)height
                                fps:(NSInteger)fps
                            quality:(NSInteger)quality
                           rotation:(NSInteger)rotation
                       portraitCrop:(BOOL)portraitCrop
                       cropYPercent:(NSInteger)cropYPercent
                    cropZoomPercent:(NSInteger)cropZoomPercent NS_DESIGNATED_INITIALIZER;

- (instancetype)initWithTCPHost:(NSString *)host
                            port:(NSInteger)port
                           width:(NSInteger)width
                          height:(NSInteger)height
                             fps:(NSInteger)fps
                         quality:(NSInteger)quality
                        rotation:(NSInteger)rotation
                    portraitCrop:(BOOL)portraitCrop
                    cropYPercent:(NSInteger)cropYPercent
                 cropZoomPercent:(NSInteger)cropZoomPercent;

- (instancetype)init NS_UNAVAILABLE;
- (BOOL)start:(NSError **)error;
- (void)stop;
- (BOOL)startDatasetRecordingInRootDirectory:(NSString *)rootDirectory
                                   targetFPS:(NSInteger)targetFPS
                                       error:(NSError **)error;
- (void)stopDatasetRecording;

@end

NS_ASSUME_NONNULL_END
