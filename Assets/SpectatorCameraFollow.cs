using UnityEngine;

public class SpectatorCameraFollow : MonoBehaviour
{
    [SerializeField] private Transform picoHead;
    [SerializeField] private Vector3 positionOffset;
    [SerializeField] private Vector3 rotationOffset;

    private void LateUpdate()
    {
        if (picoHead == null)
            return;

        transform.position = picoHead.TransformPoint(positionOffset);
        transform.rotation =
            picoHead.rotation * Quaternion.Euler(rotationOffset);
    }
}