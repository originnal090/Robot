#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

@interface IPMJCameraProducer : NSObject

- (instancetype)initWithSocketPath:(NSString *)socketPath
                              width:(NSInteger)width
                             height:(NSInteger)height
                                fps:(NSInteger)fps
                            quality:(NSInteger)quality
                           rotation:(NSInteger)rotation NS_DESIGNATED_INITIALIZER;

- (instancetype)init NS_UNAVAILABLE;
- (BOOL)start:(NSError **)error;
- (void)stop;

@end

NS_ASSUME_NONNULL_END
