using UnityEngine;

/// <summary>Pure calculations used by the PICO three-point avatar driver.</summary>
public static class PicoThreePointPoseMath
{
    public static Vector3 BodyPosition(
        Vector3 currentBodyPosition,
        Vector3 headPosition,
        Vector3 horizontalOffset)
    {
        return new Vector3(
            headPosition.x + horizontalOffset.x,
            currentBodyPosition.y,
            headPosition.z + horizontalOffset.z);
    }

    public static Quaternion BodyYaw(Quaternion headRotation, float yawOffsetDegrees)
    {
        return BodyYaw(headRotation, yawOffsetDegrees, Quaternion.identity);
    }

    public static Quaternion BodyYaw(
        Quaternion headRotation,
        float yawOffsetDegrees,
        Quaternion fallbackRotation)
    {
        Vector3 horizontalForward = Vector3.ProjectOnPlane(
            headRotation * Vector3.forward,
            Vector3.up);

        if (horizontalForward.sqrMagnitude < 0.000001f)
            return Quaternion.Euler(0f, fallbackRotation.eulerAngles.y, 0f);

        Quaternion headYaw = Quaternion.LookRotation(horizontalForward.normalized, Vector3.up);
        return headYaw * Quaternion.Euler(0f, yawOffsetDegrees, 0f);
    }

    public static Pose HandPose(
        Pose controllerPose,
        Vector3 localPositionOffset,
        Vector3 localEulerOffset)
    {
        Quaternion offsetRotation = Quaternion.Euler(localEulerOffset);
        return new Pose(
            controllerPose.position + controllerPose.rotation * localPositionOffset,
            controllerPose.rotation * offsetRotation);
    }

}
