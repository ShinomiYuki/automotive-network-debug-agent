/* 完全合成的最小生成配置，仅用于 Config MCP 路由证据测试。 */
typedef struct { unsigned short source; unsigned short destination; } PduR_Route;

const PduR_Route PduRRoutingPath_416_SU[] = {
    { IPdu_416_SU, IPdu_416_IC },
    { IPdu_416_SU, IPdu_416_GL },
};

const PduR_Route PduRRoutingPath_500_SU[] = {
    { IPdu_500_SU, IPdu_500_IC },
};

const PduR_Route PduRRoutingPath_600_SU[] = {
    { IPdu_600_SU, 0U },
};
