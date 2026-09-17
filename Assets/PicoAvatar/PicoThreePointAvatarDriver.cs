using UnityEngine;

[DefaultExecutionOrder(-50)]
public sealed class PicoThreePointAvatarDriver : MonoBehaviour
{
    [Header("Tracked Poses")]
    public Transform head;
    public Transform leftController;
    public Transform rightController;

    [Header("Avatar")]
    public Transform avatarRoot;
    public Transform leftHandTarget;
    public Transform rightHandTarget;

    [Header("Drive Switches")]
    public bool driveHands = true;
    public bool followBodyPosition = true;
    public bool followBodyYaw = true;

    [Header("Body")]
    [Min(0f)] public float bodyTurnSpeed = 180f;

    [Header("Hand Calibration")]
    public Vector3 leftHandPositionOffset;
    public Vector3 leftHandEulerOffset;
    public Vector3 rightHandPositionOffset;
    public Vector3 rightHandEulerOffset;

    private Vector3 bodyHorizontalOffset;
    private float bodyGroundHeight;
    private float bodyYawOffset;
    private bool calibrated;
    private bool missingReferenceWarningShown;

    public bool HasRequiredReferences =>
        head != null &&
        leftController != null &&
        rightController != null &&
        avatarRoot != null &&
        leftHandTarget != null &&
        rightHandTarget != null;

    private void OnEnable()
    {
        Calibrate();
    }

    private void Update()
    {
        ApplyPose(Time.deltaTime);
    }

    [ContextMenu("Calibrate From Current Pose")]
    public bool Calibrate()
    {
        if (!ValidateReferences())
            return false;

        Vector3 worldHorizontalOffset = new Vector3(
            avatarRoot.position.x - head.position.x,
            0f,
            avatarRoot.position.z - head.position.z);
        Quaternion headYaw = PicoThreePointPoseMath.BodyYaw(
            head.rotation,
            0f,
            avatarRoot.rotation);
        bodyHorizontalOffset = Quaternion.Inverse(headYaw) * worldHorizontalOffset;
        bodyGroundHeight = avatarRoot.position.y;
        bodyYawOffset = Mathf.DeltaAngle(headYaw.eulerAngles.y, avatarRoot.eulerAngles.y);
        calibrated = true;
        return true;
    }

    public void ApplyPose(float deltaTime)
    {
        if (!ValidateReferences())
            return;

        if (!calibrated && !Calibrate())
            return;

        if (followBodyPosition)
        {
            Quaternion headYaw = PicoThreePointPoseMath.BodyYaw(
                head.rotation,
                0f,
                avatarRoot.rotation);
            Vector3 worldHorizontalOffset = headYaw * bodyHorizontalOffset;
            avatarRoot.position = PicoThreePointPoseMath.BodyPosition(
                new Vector3(avatarRoot.position.x, bodyGroundHeight, avatarRoot.position.z),
                head.position,
                worldHorizontalOffset);
        }

        if (followBodyYaw)
        {
            Quaternion targetRotation = PicoThreePointPoseMath.BodyYaw(
                head.rotation,
                bodyYawOffset,
                avatarRoot.rotation);
            avatarRoot.rotation = Quaternion.RotateTowards(
                avatarRoot.rotation,
                targetRotation,
                bodyTurnSpeed * Mathf.Max(0f, deltaTime));
        }

        if (driveHands)
        {
            ApplyHand(
                leftController,
                leftHandTarget,
                leftHandPositionOffset,
                leftHandEulerOffset);
            ApplyHand(
                rightController,
                rightHandTarget,
                rightHandPositionOffset,
                rightHandEulerOffset);
        }
    }

    private static void ApplyHand(
        Transform controller,
        Transform target,
        Vector3 positionOffset,
        Vector3 eulerOffset)
    {
        Pose pose = PicoThreePointPoseMath.HandPose(
            new Pose(controller.position, controller.rotation),
            positionOffset,
            eulerOffset);
        target.SetPositionAndRotation(pose.position, pose.rotation);
    }

    private bool ValidateReferences()
    {
        if (HasRequiredReferences)
        {
            missingReferenceWarningShown = false;
            return true;
        }

        if (!missingReferenceWarningShown)
        {
            Debug.LogWarning(
                "[PicoThreePointAvatarDriver] Missing tracked-pose, avatar, or hand-target reference.",
                this);
            missingReferenceWarningShown = true;
        }

        return false;
    }
}
