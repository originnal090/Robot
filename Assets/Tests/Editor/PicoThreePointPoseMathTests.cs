using NUnit.Framework;
using UnityEngine;

public class PicoThreePointPoseMathTests
{
    [Test]
    public void BodyPositionFollowsHeadOnlyOnHorizontalPlane()
    {
        Vector3 result = PicoThreePointPoseMath.BodyPosition(
            new Vector3(1f, -0.07f, 2f),
            new Vector3(4f, 1.75f, 8f),
            new Vector3(-0.25f, 0f, 0.1f));

        Assert.That(result, Is.EqualTo(new Vector3(3.75f, -0.07f, 8.1f)));
    }

    [Test]
    public void BodyYawIgnoresHeadPitchAndRoll()
    {
        Quaternion result = PicoThreePointPoseMath.BodyYaw(
            Quaternion.Euler(35f, 70f, 22f), 15f);

        Assert.That(Mathf.DeltaAngle(result.eulerAngles.y, 85f), Is.EqualTo(0f).Within(0.001f));
        Assert.That(Mathf.DeltaAngle(result.eulerAngles.x, 0f), Is.EqualTo(0f).Within(0.001f));
        Assert.That(Mathf.DeltaAngle(result.eulerAngles.z, 0f), Is.EqualTo(0f).Within(0.001f));
    }

    [Test]
    public void BodyYawUsesFallbackWhenHeadForwardIsVertical()
    {
        Quaternion lookingStraightUp = Quaternion.LookRotation(Vector3.up, Vector3.forward);
        Quaternion result = PicoThreePointPoseMath.BodyYaw(
            lookingStraightUp,
            15f,
            Quaternion.Euler(0f, 123f, 0f));

        Assert.That(Mathf.DeltaAngle(result.eulerAngles.y, 123f), Is.EqualTo(0f).Within(0.001f));
        Assert.That(Mathf.DeltaAngle(result.eulerAngles.x, 0f), Is.EqualTo(0f).Within(0.001f));
        Assert.That(Mathf.DeltaAngle(result.eulerAngles.z, 0f), Is.EqualTo(0f).Within(0.001f));
    }

    [Test]
    public void HandPoseAppliesOffsetInControllerSpace()
    {
        Pose result = PicoThreePointPoseMath.HandPose(
            new Pose(new Vector3(1f, 2f, 3f), Quaternion.Euler(0f, 90f, 0f)),
            new Vector3(0f, 0f, 0.2f),
            new Vector3(0f, 0f, 90f));

        Assert.That(result.position.x, Is.EqualTo(1.2f).Within(0.001f));
        Assert.That(result.position.y, Is.EqualTo(2f).Within(0.001f));
        Assert.That(result.position.z, Is.EqualTo(3f).Within(0.001f));
        Assert.That(
            Quaternion.Angle(
                result.rotation,
                Quaternion.Euler(0f, 90f, 0f) * Quaternion.Euler(0f, 0f, 90f)),
            Is.LessThan(0.001f));
    }

}
