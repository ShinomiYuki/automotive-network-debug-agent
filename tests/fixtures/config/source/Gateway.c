/* 完全合成的普通源码上下文，用于验证有限行返回和多位置匹配。 */
void Gateway_Init(void)
{
    RegisterRoute(PduRRoutingPath_416_SU);
}

void Gateway_Check(void)
{
    CheckRoute(PduRRoutingPath_416_SU);
}

#define VEHICLE_SPEED_TIMEOUT_MS 500U

static unsigned short VehicleSpeed_Rx;
static unsigned short VehicleSpeed_Tx;

void VehicleSpeed_GW(void)
{
    VehicleSpeed_Tx = VehicleSpeed_Rx;
}

void Start_TxGroup(void)
{
    Com_IpduGroupStart(TxGroup, false);
}
