using System;
using System.Collections.Generic;

public static class RobotVideoSource
{
    public static string BuildStreamUrl(string robotIp, int videoPort)
    {
        ValidateEndpoint(robotIp, videoPort);
        return string.Format("http://{0}:{1}/?action=stream", robotIp.Trim(), videoPort);
    }

    public static string BuildSnapshotUrl(string robotIp, int videoPort)
    {
        ValidateEndpoint(robotIp, videoPort);
        return string.Format("http://{0}:{1}/?action=snapshot", robotIp.Trim(), videoPort);
    }

    private static void ValidateEndpoint(string robotIp, int videoPort)
    {
        if (string.IsNullOrWhiteSpace(robotIp))
            throw new ArgumentException("Robot IP is required.", "robotIp");

        if (videoPort < 1 || videoPort > 65535)
            throw new ArgumentOutOfRangeException("videoPort");

    }
}

public sealed class MjpegFrameParser
{
    private readonly int maximumFrameBytes;
    private readonly List<byte> currentFrame = new List<byte>();
    private bool insideFrame;
    private byte previousByte;

    public MjpegFrameParser(int maximumFrameBytes = 2 * 1024 * 1024)
    {
        if (maximumFrameBytes < 4)
            throw new ArgumentOutOfRangeException("maximumFrameBytes");
        this.maximumFrameBytes = maximumFrameBytes;
    }

    public void Feed(byte[] bytes, int count, Action<byte[]> onFrame)
    {
        if (bytes == null)
            throw new ArgumentNullException("bytes");
        if (count < 0 || count > bytes.Length)
            throw new ArgumentOutOfRangeException("count");
        if (onFrame == null)
            throw new ArgumentNullException("onFrame");

        for (int index = 0; index < count; index++)
        {
            byte value = bytes[index];
            if (!insideFrame)
            {
                if (previousByte == 0xFF && value == 0xD8)
                {
                    currentFrame.Clear();
                    currentFrame.Add(0xFF);
                    currentFrame.Add(0xD8);
                    insideFrame = true;
                }
                previousByte = value;
                continue;
            }

            currentFrame.Add(value);
            if (currentFrame.Count > maximumFrameBytes)
            {
                Reset();
                continue;
            }

            if (previousByte == 0xFF && value == 0xD9)
            {
                onFrame(currentFrame.ToArray());
                Reset();
                continue;
            }

            previousByte = value;
        }
    }

    private void Reset()
    {
        currentFrame.Clear();
        insideFrame = false;
        previousByte = 0;
    }
}

public sealed class RobotVideoFrameSlot
{
    private readonly object gate = new object();
    private byte[] latest;

    public void Publish(byte[] jpegBytes)
    {
        if (jpegBytes == null || jpegBytes.Length == 0)
            return;

        lock (gate)
        {
            latest = jpegBytes;
        }
    }

    public bool TryTakeLatest(out byte[] jpegBytes)
    {
        lock (gate)
        {
            jpegBytes = latest;
            latest = null;
            return jpegBytes != null;
        }
    }

    public void Clear()
    {
        lock (gate)
        {
            latest = null;
        }
    }
}
