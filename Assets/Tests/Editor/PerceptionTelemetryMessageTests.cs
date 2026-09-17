using NUnit.Framework;
using UnityEngine;

public sealed class PerceptionTelemetryMessageTests
{
    [Test]
    public void ParsesSupportedPacket()
    {
        const string json =
            "{\"type\":\"perception\",\"schema_version\":1," +
            "\"source\":\"unit_test\",\"session_id\":\"session-1\"," +
            "\"seq\":3,\"sent_at_ms\":1234,\"state\":\"detected\"," +
            "\"video_ok\":true,\"detected\":true,\"candidate_detected\":true," +
            "\"target\":\"red_ball\",\"center_x\":216.0,\"center_y\":231.0," +
            "\"radius\":6.2,\"score\":0.4,\"circularity\":0.8," +
            "\"aspect_ratio\":1.1,\"frame_width\":640,\"frame_height\":480," +
            "\"processing_ms\":5.5}";

        PerceptionTelemetryMessage message =
            JsonUtility.FromJson<PerceptionTelemetryMessage>(json);

        Assert.That(message.IsSupported(), Is.True);
        Assert.That(message.detected, Is.True);
        Assert.That(message.center_x, Is.EqualTo(216.0f));
        Assert.That(message.seq, Is.EqualTo(3));
    }

    [Test]
    public void RejectsUnexpectedSchema()
    {
        const string json =
            "{\"type\":\"perception\",\"schema_version\":2," +
            "\"session_id\":\"session-1\",\"seq\":1}";
        PerceptionTelemetryMessage message =
            JsonUtility.FromJson<PerceptionTelemetryMessage>(json);
        Assert.That(message.IsSupported(), Is.False);
    }
}
