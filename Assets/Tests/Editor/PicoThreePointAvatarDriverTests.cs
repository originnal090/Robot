using System;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

public class PicoThreePointAvatarDriverTests
{
    private GameObject root;
    private PicoThreePointAvatarDriver driver;

    [SetUp]
    public void SetUp()
    {
        root = new GameObject("TestRoot");
        driver = root.AddComponent<PicoThreePointAvatarDriver>();
        driver.enabled = false;
        driver.head = NewTransform("Head", new Vector3(0f, 1.7f, 0f));
        driver.leftController = NewTransform("LeftController", new Vector3(-0.3f, 1.2f, 0.4f));
        driver.rightController = NewTransform("RightController", new Vector3(0.3f, 1.2f, 0.4f));
        driver.avatarRoot = NewTransform("Avatar", new Vector3(0f, -0.07f, -0.25f));
        driver.leftHandTarget = NewTransform("LeftTarget", Vector3.zero);
        driver.rightHandTarget = NewTransform("RightTarget", Vector3.zero);
    }

    [TearDown]
    public void TearDown()
    {
        UnityEngine.Object.DestroyImmediate(root);
    }

    [Test]
    public void CalibratedBodyFollowsHeadHorizontallyAndKeepsFloorHeight()
    {
        driver.avatarRoot.rotation = Quaternion.Euler(0f, 10f, 0f);
        driver.head.rotation = Quaternion.Euler(0f, 20f, 0f);
        Assert.That(driver.Calibrate(), Is.True);

        driver.head.position = new Vector3(0.5f, 1.2f, 1f);
        driver.head.rotation = Quaternion.Euler(35f, 50f, 12f);
        driver.ApplyPose(1f);

        Assert.That(driver.avatarRoot.position.x, Is.EqualTo(0.375f).Within(0.001f));
        Assert.That(driver.avatarRoot.position.y, Is.EqualTo(-0.07f).Within(0.001f));
        Assert.That(driver.avatarRoot.position.z, Is.EqualTo(0.7834936f).Within(0.001f));
        Assert.That(
            Mathf.DeltaAngle(driver.avatarRoot.eulerAngles.y, 40f),
            Is.EqualTo(0f).Within(0.001f));
    }

    [Test]
    public void HandsUseMatchingControllerPoseAndConfiguredOffsets()
    {
        driver.leftController.rotation = Quaternion.Euler(0f, 90f, 0f);
        driver.rightController.rotation = Quaternion.Euler(0f, -90f, 0f);
        driver.leftHandPositionOffset = new Vector3(0f, 0f, 0.1f);
        driver.rightHandPositionOffset = new Vector3(0f, 0f, 0.2f);
        driver.leftHandEulerOffset = new Vector3(0f, 0f, 10f);
        driver.rightHandEulerOffset = new Vector3(0f, 0f, -10f);
        driver.Calibrate();

        driver.ApplyPose(1f);

        Pose expectedLeft = PicoThreePointPoseMath.HandPose(
            new Pose(driver.leftController.position, driver.leftController.rotation),
            driver.leftHandPositionOffset,
            driver.leftHandEulerOffset);
        Pose expectedRight = PicoThreePointPoseMath.HandPose(
            new Pose(driver.rightController.position, driver.rightController.rotation),
            driver.rightHandPositionOffset,
            driver.rightHandEulerOffset);

        Assert.That(
            Vector3.Distance(driver.leftHandTarget.position, expectedLeft.position),
            Is.LessThan(0.001f));
        Assert.That(
            Vector3.Distance(driver.rightHandTarget.position, expectedRight.position),
            Is.LessThan(0.001f));
        Assert.That(
            Quaternion.Angle(driver.leftHandTarget.rotation, expectedLeft.rotation),
            Is.LessThan(0.001f));
        Assert.That(
            Quaternion.Angle(driver.rightHandTarget.rotation, expectedRight.rotation),
            Is.LessThan(0.001f));
    }

    [Test]
    public void CalibratedBodyOffsetRotatesWithHeadYaw()
    {
        driver.head.rotation = Quaternion.identity;
        Assert.That(driver.Calibrate(), Is.True);

        driver.head.rotation = Quaternion.Euler(0f, 90f, 0f);
        driver.ApplyPose(1f);

        Assert.That(driver.avatarRoot.position.x, Is.EqualTo(-0.25f).Within(0.001f));
        Assert.That(driver.avatarRoot.position.z, Is.EqualTo(0f).Within(0.001f));
    }

    [Test]
    public void CalibratedBodyRestoresCapturedGroundHeight()
    {
        Assert.That(driver.Calibrate(), Is.True);
        driver.avatarRoot.position = new Vector3(0f, 5f, -0.25f);

        driver.ApplyPose(1f);

        Assert.That(driver.avatarRoot.position.y, Is.EqualTo(-0.07f).Within(0.001f));
    }

    [Test]
    public void MissingReferencePreventsAnyPoseWrite()
    {
        driver.rightController = null;
        Vector3 originalPosition = driver.avatarRoot.position;

        Assert.That(driver.HasRequiredReferences, Is.False);
        LogAssert.Expect(
            LogType.Warning,
            "[PicoThreePointAvatarDriver] Missing tracked-pose, avatar, or hand-target reference.");
        Assert.That(driver.Calibrate(), Is.False);
        driver.ApplyPose(1f);

        Assert.That(driver.avatarRoot.position, Is.EqualTo(originalPosition));
    }

    [Test]
    public void DriverRunsAfterXrLocomotionAndBeforeRigBuilder()
    {
        var order = (DefaultExecutionOrder)Attribute.GetCustomAttribute(
            typeof(PicoThreePointAvatarDriver),
            typeof(DefaultExecutionOrder));

        Assert.That(order, Is.Not.Null);
        Assert.That(order.order, Is.EqualTo(-50));
    }

    [Test]
    public void CalibrationAtVerticalGazeUsesStableBodyYawFallback()
    {
        driver.avatarRoot.rotation = Quaternion.Euler(0f, 123f, 0f);
        driver.head.rotation = Quaternion.LookRotation(Vector3.up, Vector3.forward);
        Assert.That(driver.Calibrate(), Is.True);

        driver.head.rotation = Quaternion.identity;
        driver.ApplyPose(1f);

        Assert.That(
            Mathf.DeltaAngle(driver.avatarRoot.eulerAngles.y, 0f),
            Is.EqualTo(0f).Within(0.001f));
    }


    private Transform NewTransform(string name, Vector3 position)
    {
        var child = new GameObject(name);
        child.transform.SetParent(root.transform);
        child.transform.position = position;
        return child.transform;
    }
}
