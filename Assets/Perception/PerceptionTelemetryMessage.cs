using System;
using UnityEngine.Events;

[Serializable]
public sealed class PerceptionTelemetryMessage
{
    public string type;
    public int schema_version;
    public string source;
    public string session_id;
    public long seq;
    public long sent_at_ms;
    public string state;
    public bool video_ok;
    public bool detected;
    public bool candidate_detected;
    public string target;
    public float center_x;
    public float center_y;
    public float radius;
    public float score;
    public float circularity;
    public float aspect_ratio;
    public int frame_width;
    public int frame_height;
    public float processing_ms;

    public bool IsSupported()
    {
        return string.Equals(type, "perception", StringComparison.Ordinal) &&
               schema_version == 1 &&
               !string.IsNullOrEmpty(session_id) &&
               seq > 0;
    }
}

[Serializable]
public sealed class PerceptionTelemetryEvent : UnityEvent<PerceptionTelemetryMessage>
{
}
