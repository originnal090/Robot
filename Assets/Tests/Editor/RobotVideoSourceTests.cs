using NUnit.Framework;

public sealed class RobotVideoSourceTests
{
    [Test]
    public void BuildSnapshotUrlUsesOrangePiSnapshotEndpoint()
    {
        string url = RobotVideoSource.BuildSnapshotUrl("192.168.137.106", 8080);

        Assert.AreEqual(
            "http://192.168.137.106:8080/?action=snapshot",
            url);
    }

    [Test]
    public void BuildStreamUrlUsesOrangePiStreamEndpoint()
    {
        string url = RobotVideoSource.BuildStreamUrl("192.168.137.106", 8080);

        Assert.AreEqual(
            "http://192.168.137.106:8080/?action=stream",
            url);
    }

    [Test]
    public void MjpegParserPublishesFrameSplitAcrossChunks()
    {
        var parser = new MjpegFrameParser();
        byte[] published = null;

        parser.Feed(new byte[] { 0x00, 0xFF }, 2, frame => published = frame);
        parser.Feed(new byte[] { 0xD8, 0x11, 0xFF, 0xD9 }, 4, frame => published = frame);

        CollectionAssert.AreEqual(
            new byte[] { 0xFF, 0xD8, 0x11, 0xFF, 0xD9 },
            published);
    }

    [Test]
    public void LatestFrameSlotReturnsOnlyNewestFrame()
    {
        var slot = new RobotVideoFrameSlot();
        slot.Publish(new byte[] { 1 });
        slot.Publish(new byte[] { 2 });

        Assert.IsTrue(slot.TryTakeLatest(out byte[] frame));
        CollectionAssert.AreEqual(new byte[] { 2 }, frame);
        Assert.IsFalse(slot.TryTakeLatest(out _));
    }
}
