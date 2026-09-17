using System;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.UI;

public sealed class RobotVideoReceiver : MonoBehaviour
{
    [Header("TonyPi 原厂视频")]
    public string robotIp = "192.168.137.106";
    [Min(1)] public int videoPort = 8080;
    [Min(0.1f)] public float reconnectDelaySeconds = 1f;
    [Min(1f)] public float fallbackSnapshotFps = 8f;
    [Min(1f)] public int requestTimeoutSeconds = 3;
    public bool bypassSystemProxy = true;

    [Header("Unity 小窗")]
    public RawImage targetImage;
    public bool autoCreateWindow = true;
    public bool worldSpaceForVr = true;
    public Vector2 windowSize = new Vector2(360f, 202.5f);
    public Vector2 topRightMargin = new Vector2(24f, 24f);
    public Vector3 worldSpaceLocalPosition = new Vector3(0.45f, 0.25f, 1.2f);
    [Min(0.0001f)] public float worldSpaceScale = 0.001f;
    public int canvasSortOrder = 50;

    private Texture2D currentTexture;
    private AspectRatioFitter aspectRatioFitter;
    private GameObject generatedCanvas;
    private readonly RobotVideoFrameSlot pendingFrames = new RobotVideoFrameSlot();
    private HttpClient httpClient;
    private CancellationTokenSource streamCancellation;
    private Task streamTask;
    private string lastError;
    private string pendingError;

    private void OnEnable()
    {
        EnsureTargetImage();
        HttpClientHandler handler = new HttpClientHandler
        {
            UseProxy = !bypassSystemProxy
        };
        httpClient = new HttpClient(handler);
        httpClient.Timeout = Timeout.InfiniteTimeSpan;
        streamCancellation = new CancellationTokenSource();
        string streamUrl = RobotVideoSource.BuildStreamUrl(robotIp, videoPort);
        string snapshotUrl = RobotVideoSource.BuildSnapshotUrl(robotIp, videoPort);
        streamTask = Task.Run(
            () => StreamFramesAsync(streamUrl, snapshotUrl, streamCancellation.Token));
    }

    private void OnDisable()
    {
        if (streamCancellation != null)
        {
            streamCancellation.Cancel();
            streamCancellation.Dispose();
            streamCancellation = null;
        }

        if (httpClient != null)
        {
            httpClient.Dispose();
            httpClient = null;
        }

        streamTask = null;

        if (currentTexture != null)
        {
            Destroy(currentTexture);
            currentTexture = null;
        }

        pendingFrames.Clear();

        if (generatedCanvas != null)
        {
            Destroy(generatedCanvas);
            generatedCanvas = null;
        }
    }

    private void Update()
    {
        string error = Interlocked.Exchange(ref pendingError, null);
        if (!string.IsNullOrEmpty(error) && error != lastError)
        {
            lastError = error;
            Debug.LogWarning(string.Format("[ROBOT VIDEO] {0}", error));
        }

        byte[] jpegBytes;
        if (!pendingFrames.TryTakeLatest(out jpegBytes))
            return;

        Texture2D nextTexture = new Texture2D(2, 2, TextureFormat.RGBA32, false);
        if (!nextTexture.LoadImage(jpegBytes, false))
        {
            Destroy(nextTexture);
            return;
        }

        ReplaceTexture(nextTexture);
        lastError = null;
    }

    private async Task StreamFramesAsync(
        string streamUrl,
        string snapshotUrl,
        CancellationToken stopToken)
    {
        while (!stopToken.IsCancellationRequested)
        {
            try
            {
                HttpResponseMessage response;
                using (var connectCancellation = CancellationTokenSource.CreateLinkedTokenSource(stopToken))
                {
                    connectCancellation.CancelAfter(TimeSpan.FromSeconds(requestTimeoutSeconds));
                    response = await httpClient.GetAsync(
                        streamUrl,
                        HttpCompletionOption.ResponseHeadersRead,
                        connectCancellation.Token).ConfigureAwait(false);
                }

                using (response)
                {
                    response.EnsureSuccessStatusCode();
                    Interlocked.Exchange(ref pendingError, null);
                    var parser = new MjpegFrameParser();
                    using (System.IO.Stream stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                    {
                        byte[] buffer = new byte[16 * 1024];
                        while (!stopToken.IsCancellationRequested)
                        {
                            int read;
                            using (var readCancellation = CancellationTokenSource.CreateLinkedTokenSource(stopToken))
                            {
                                readCancellation.CancelAfter(TimeSpan.FromSeconds(requestTimeoutSeconds));
                                try
                                {
                                    read = await stream.ReadAsync(
                                        buffer,
                                        0,
                                        buffer.Length,
                                        readCancellation.Token).ConfigureAwait(false);
                                }
                                catch (OperationCanceledException) when (!stopToken.IsCancellationRequested)
                                {
                                    throw new TimeoutException("edge video stream timed out");
                                }
                            }
                            if (read == 0)
                                throw new System.IO.EndOfStreamException("edge video stream closed");
                            parser.Feed(buffer, read, pendingFrames.Publish);
                        }
                    }
                }
            }
            catch (OperationCanceledException) when (stopToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                Interlocked.Exchange(
                    ref pendingError,
                    string.Format("stream unavailable: {0} ({1})", exception.GetBaseException().Message, streamUrl));
            }

            try
            {
                await PollSnapshotsUntilStreamRetryAsync(snapshotUrl, stopToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    private async Task PollSnapshotsUntilStreamRetryAsync(
        string snapshotUrl,
        CancellationToken stopToken)
    {
        DateTime retryAt = DateTime.UtcNow.AddSeconds(Math.Max(0.1, reconnectDelaySeconds));
        TimeSpan interval = TimeSpan.FromSeconds(1.0 / Math.Max(1.0, fallbackSnapshotFps));

        do
        {
            try
            {
                byte[] jpegBytes = await DownloadSnapshotAsync(snapshotUrl, stopToken).ConfigureAwait(false);
                if (jpegBytes != null && jpegBytes.Length > 0)
                {
                    pendingFrames.Publish(jpegBytes);
                    Interlocked.Exchange(ref pendingError, null);
                }
            }
            catch (OperationCanceledException) when (stopToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception exception)
            {
                Interlocked.Exchange(
                    ref pendingError,
                    string.Format("stream and snapshot unavailable: {0}", exception.GetBaseException().Message));
            }

            TimeSpan remaining = retryAt - DateTime.UtcNow;
            if (remaining <= TimeSpan.Zero)
                break;
            await Task.Delay(remaining < interval ? remaining : interval, stopToken).ConfigureAwait(false);
        }
        while (!stopToken.IsCancellationRequested);
    }

    private async Task<byte[]> DownloadSnapshotAsync(
        string snapshotUrl,
        CancellationToken stopToken)
    {
        using (var requestCancellation = CancellationTokenSource.CreateLinkedTokenSource(stopToken))
        {
            requestCancellation.CancelAfter(TimeSpan.FromSeconds(requestTimeoutSeconds));
            using (HttpResponseMessage response = await httpClient.GetAsync(
                snapshotUrl,
                HttpCompletionOption.ResponseContentRead,
                requestCancellation.Token).ConfigureAwait(false))
            {
                response.EnsureSuccessStatusCode();
                return await response.Content.ReadAsByteArrayAsync().ConfigureAwait(false);
            }
        }
    }

    private void ReplaceTexture(Texture2D nextTexture)
    {
        Texture2D previousTexture = currentTexture;
        currentTexture = nextTexture;

        if (targetImage != null)
            targetImage.texture = currentTexture;

        if (aspectRatioFitter != null && currentTexture.height > 0)
            aspectRatioFitter.aspectRatio = (float)currentTexture.width / currentTexture.height;

        if (previousTexture != null)
            Destroy(previousTexture);
    }

    private void EnsureTargetImage()
    {
        if (targetImage != null || !autoCreateWindow)
            return;

        generatedCanvas = new GameObject(
            "RobotVideoCanvas",
            typeof(Canvas),
            typeof(CanvasScaler),
            typeof(GraphicRaycaster));

        Canvas canvas = generatedCanvas.GetComponent<Canvas>();
        Camera mainCamera = Camera.main;
        bool useWorldSpace = worldSpaceForVr && mainCamera != null;
        canvas.renderMode = useWorldSpace ? RenderMode.WorldSpace : RenderMode.ScreenSpaceOverlay;
        canvas.sortingOrder = canvasSortOrder;

        if (useWorldSpace)
        {
            generatedCanvas.transform.SetParent(mainCamera.transform, false);
            generatedCanvas.transform.localPosition = worldSpaceLocalPosition;
            generatedCanvas.transform.localRotation = Quaternion.identity;
            generatedCanvas.transform.localScale = Vector3.one * worldSpaceScale;
            canvas.worldCamera = mainCamera;
        }

        CanvasScaler scaler = generatedCanvas.GetComponent<CanvasScaler>();
        scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
        scaler.referenceResolution = new Vector2(1920f, 1080f);
        scaler.screenMatchMode = CanvasScaler.ScreenMatchMode.MatchWidthOrHeight;
        scaler.matchWidthOrHeight = 0.5f;

        GameObject window = new GameObject("RobotVideoWindow", typeof(RectTransform), typeof(Image));
        window.transform.SetParent(generatedCanvas.transform, false);

        RectTransform windowRect = window.GetComponent<RectTransform>();
        windowRect.anchorMin = useWorldSpace ? new Vector2(0.5f, 0.5f) : new Vector2(1f, 1f);
        windowRect.anchorMax = useWorldSpace ? new Vector2(0.5f, 0.5f) : new Vector2(1f, 1f);
        windowRect.pivot = useWorldSpace ? new Vector2(0.5f, 0.5f) : new Vector2(1f, 1f);
        windowRect.sizeDelta = windowSize;
        windowRect.anchoredPosition = useWorldSpace
            ? Vector2.zero
            : new Vector2(-topRightMargin.x, -topRightMargin.y);

        Image background = window.GetComponent<Image>();
        background.color = new Color(0.03f, 0.04f, 0.05f, 0.92f);

        GameObject imageObject = new GameObject("RobotVideoImage", typeof(RectTransform), typeof(RawImage));
        imageObject.transform.SetParent(window.transform, false);

        RectTransform imageRect = imageObject.GetComponent<RectTransform>();
        imageRect.anchorMin = Vector2.zero;
        imageRect.anchorMax = Vector2.one;
        imageRect.offsetMin = new Vector2(4f, 4f);
        imageRect.offsetMax = new Vector2(-4f, -4f);

        targetImage = imageObject.GetComponent<RawImage>();
        targetImage.color = Color.white;
        aspectRatioFitter = imageObject.AddComponent<AspectRatioFitter>();
        aspectRatioFitter.aspectMode = AspectRatioFitter.AspectMode.FitInParent;
        aspectRatioFitter.aspectRatio = 4f / 3f;
    }
}
